"""Live capture of the game window -> GameState updates (on a worker thread)."""

from __future__ import annotations

import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Signal

from . import gamedata
from .kills import parse_message
from .vision.reader import GameState, Reader

WINDOW_NAME = "MapleStory"
MESSAGES_EVERY_S = 0.3
STATUS_EVERY_S = 1.0
MAP_EVERY_S = 3.0


class CaptureWorker(QObject):
    status_read = Signal(int, int, float)  # level, exp, percent
    map_read = Signal(str, object)  # map display name, tuple[Mob, ...]
    messages_read = Signal(object)  # list[TextLine]
    price_items = Signal(object)  # list[(name, item_id)], hovered first
    info = Signal(str)

    def __init__(self, window_name: str = WINDOW_NAME, parent=None):
        super().__init__(parent)
        self.window_name = window_name
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._running = False
        self._map_key: tuple[str, str] | None = None
        self._price_request: tuple[int, int] | None = None

    # --- capture --------------------------------------------------------------

    def start(self) -> None:
        from windows_capture import WindowsCapture

        self._running = True
        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=100,  # ms; we never need more than ~10 fps
            window_name=self.window_name,
        )

        @cap.event
        def on_frame_arrived(frame, control):
            if not self._running:
                control.stop()
                return
            buf = frame.frame_buffer  # BGRA
            with self._lock:
                self._frame = np.ascontiguousarray(buf[:, :, :3])

        @cap.event
        def on_closed():
            self.info.emit("Game window closed")

        try:
            self._control = cap.start_free_threaded()
        except Exception as e:  # window not found, etc.
            self.info.emit(f"Can't capture '{self.window_name}': {e}")
            self._running = False
            return
        threading.Thread(target=self._loop, name="reader", daemon=True).start()
        self.info.emit(f"Watching '{self.window_name}'")

    def stop(self) -> None:
        self._running = False

    def request_price_check(self) -> None:
        from .hotkey import cursor_in_window

        self._price_request = cursor_in_window(self.window_name) or (0, 0)

    # --- processing -----------------------------------------------------------

    def _latest(self) -> np.ndarray | None:
        with self._lock:
            return self._frame

    def _loop(self) -> None:
        try:
            self._run()
        except Exception as e:  # surface crashes instead of dying silently
            import traceback

            traceback.print_exc()
            self.info.emit(f"Reader crashed: {e}")

    def _run(self) -> None:
        reader = Reader()
        next_msg = next_status = next_map = 0.0
        while self._running:
            frame = self._latest()
            now = time.monotonic()
            if frame is None:
                time.sleep(0.1)
                continue
            if self._price_request is not None:
                cursor, self._price_request = self._price_request, None
                self._price_check(reader, frame, cursor)
                continue
            s = GameState()
            if now >= next_msg:
                next_msg = now + MESSAGES_EVERY_S
                reader.read_messages(frame, s)
                for line in s.messages:  # warm item-id cache off the UI thread
                    m = parse_message(line.text)
                    if m and m.kind == "loot":
                        gamedata.item_id(m.item)
                self.messages_read.emit(s.messages)
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                reader.read_status(frame, s)
                if s.level and s.exp is not None and s.exp_percent is not None:
                    self.status_read.emit(s.level, s.exp, s.exp_percent)
            if now >= next_map:
                next_map = now + MAP_EVERY_S
                reader.read_map(frame, s)
                self._resolve_map(s.street or "", s.map_name or "")
            time.sleep(0.02)

    def _resolve_map(self, street: str, name: str) -> None:
        if not name or (street, name) == self._map_key:
            return
        try:
            m = gamedata.find_map(street, name)
            if m is None:
                return
            mobs = gamedata.map_mobs(m["id"])
        except Exception as e:
            self.info.emit(f"Map lookup failed: {e}")
            return
        self._map_key = (street, name)
        self.map_read.emit(f"{m['streetName']} : {m['name']}", mobs)

    def _price_check(self, reader: Reader, frame, cursor: tuple[int, int]) -> None:
        from .vision.tooltip import hovered_item_names

        self.info.emit("Reading item tooltip...")
        items = hovered_item_names(reader, frame, cursor, gamedata.item_id)
        if not items:
            self.info.emit("No item tooltip found under the cursor")
        self.price_items.emit(items)
