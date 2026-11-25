"""Live capture of the game window -> GameState updates (on a worker thread)."""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

import numpy as np
from PySide6.QtCore import QObject, Signal

from . import gamedata
from .kills import parse_message
from .vision.quickslots import QuickslotReader
from .vision.reader import GameState, Reader, crop, parse_level, parse_status_exp

WINDOW_NAME = "MapleStory"
MESSAGES_EVERY_S = 0.3
STATUS_EVERY_S = 1.0
MAP_EVERY_S = 3.0
CHAT_EVERY_S = 2.0  # chat box: only for party join/leave lines
QUICKSLOTS_EVERY_S = 1.0


_EXP_BEFORE_BRACKET = re.compile(r"(\d[\d,.\s]*)\s*\[")


def parse_status_text(text: str) -> tuple[int, float | None] | None:
    """'EXP.66,700[14.24%]' -> (66700, 14.24); with the percent unreadable
    ('EXP.66,700[1?.2') -> (66700, None). The EXP must end at the '[' so a
    lost bracket can't glue the percent digits onto it."""
    if p := parse_status_exp(text):
        return p
    m = _EXP_BEFORE_BRACKET.search(text.replace("EXP", "", 1))
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return (int(digits), None) if digits else None


def read_status(reader: Reader, frame, state: GameState) -> None:
    """Like ``Reader.read_status``, but keeps the EXP when only the percent failed."""
    state.level = parse_level([reader._line(crop(frame, "level"))])
    if p := parse_status_text(reader._line(crop(frame, "status_exp"))):
        state.exp, state.exp_percent = p


class CaptureWorker(QObject):
    status_read = Signal(object, int, object)  # level, exp, percent (level/percent None if unreadable)
    map_read = Signal(str, object)  # map display name, tuple[Mob, ...]
    messages_read = Signal(object)  # list[TextLine]
    party_read = Signal(object)  # list[str] party member names; empty = no party
    chat_read = Signal(object)  # list[TextLine] in the chat box
    quickslots_read = Signal(object)  # list[SlotRead], one per quickslot
    price_items = Signal(object)  # list[(name, item_id)], hovered first
    info = Signal(str)

    def __init__(self, window_name: str = WINDOW_NAME, parent=None):
        super().__init__(parent)
        self.window_name = window_name
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._running = False
        self._map_key: tuple[str, str] | None = None
        self.hwnd: int | None = None
        self._price_request: tuple[int, int] | None = None
        # Quickslot OCR only runs while this says so (the experimental items-used setting).
        self.read_quickslots: Callable[[], bool] = lambda: False

    # --- capture --------------------------------------------------------------

    def start(self) -> None:
        from windows_capture import WindowsCapture

        from .winutil import find_game_window

        hwnd = find_game_window(title=self.window_name)
        if hwnd is None:
            self.info.emit("Game window not found: start MapleStory, then restart the tracker")
            return
        self.hwnd = hwnd
        try:
            cap = WindowsCapture(
                cursor_capture=False,
                draw_border=False,
                minimum_update_interval=100,  # ms; we never need more than ~10 fps
                window_hwnd=hwnd,
            )
        except Exception as e:  # e.g. windows-capture 1.x has no window_hwnd
            self.info.emit(f"Can't capture the game window: {e}")
            return
        self._running = True

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
        self._thread = threading.Thread(target=self._loop, name="reader", daemon=True)
        self._thread.start()
        self.info.emit(f"Watching '{self.window_name}'")

    def stop(self) -> None:
        """Stop capturing and wait briefly for the reader thread to exit."""
        self._running = False
        control = getattr(self, "_control", None)
        if control is not None:
            try:
                control.stop()
            except Exception:
                pass
        thread = getattr(self, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def request_price_check(self) -> None:
        from .hotkey import cursor_in_window

        self._price_request = cursor_in_window(getattr(self, "hwnd", None)) or (0, 0)

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
        slots: QuickslotReader | None = None  # made on first use: not at all while the setting is off
        next_msg = next_status = next_map = next_slots = next_chat = 0.0
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
                read_status(reader, frame, s)
                if s.exp is not None:  # unreadable level digits pass through as None
                    self.status_read.emit(s.level or None, s.exp, s.exp_percent)
            if now >= next_slots:
                next_slots = now + QUICKSLOTS_EVERY_S
                reads = None
                if not self.read_quickslots():
                    slots = None  # off: no OCR, and a fresh reader when it's turned back on
                else:
                    if slots is None:
                        slots = QuickslotReader(reader.ocr)
                    reads = slots.read(frame)
                if reads:
                    self.quickslots_read.emit(reads)
            if now >= next_chat:
                next_chat = now + CHAT_EVERY_S
                reader.read_chat(frame, s)
                self.chat_read.emit(s.chat)
            if now >= next_map:
                next_map = now + MAP_EVERY_S
                reader.read_map(frame, s)
                reader.read_party(frame, s)
                self.party_read.emit(s.party)
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
