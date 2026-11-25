"""Live capture of the game window -> GameState updates (on a worker thread)."""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Signal

from . import gamedata
from .kills import parse_message
from .vision.quickslots import QuickslotReader
from .vision.reader import GameState, Reader, crop, parse_level, parse_status_exp

log = logging.getLogger(__name__)
FRAME_ERROR_LOG_EVERY_S = 30.0  # a repeating per-frame error: log once per this many seconds

WINDOW_NAME = "MapleStory"
MESSAGES_EVERY_S = 0.15  # newest rows, recognition only (``read_messages_fast``)
MESSAGES_FULL_EVERY_S = 3.0  # with text detection: re-measures the rows, a fallback
STATUS_EVERY_S = 1.0
MAP_EVERY_S = 3.0
# A different map (or a raw, unresolved read) must hold this many reads in a row,
# or this long, before it replaces the shown one: one noisy frame mustn't end
# the session (#34 review).
MAP_CONFIRM_READS = 3
MAP_CONFIRM_S = 1.0
RAW_MAP_MARK = " (?)"  # suffix on a raw read shown with no mobs: never equal to a resolved name
PLAYFIELD_S = 3.0  # an EXP read this recent means the frame is the game, not a login/loading screen
CHAT_EVERY_S = 5.0  # chat box: only for party join/leave lines (a line stays in the 3-line strip until 3 newer chat lines push it out)
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
    party_read = Signal(int)  # party members (HP-bar rows); 0 = no party widget
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
        self._lookup_key: tuple[str, str, bool] | None = None
        self._lookup: tuple[dict | None, str | None, tuple] = (None, None, ())
        self._map_shown: tuple[str, str, bool] | None = None  # (display name, place, resolved)
        self._map_pending: tuple[str, float, int] | None = None  # (display name, first seen, reads)
        self._exp_seen_at: float | None = None
        # The minimap's state for a prompt to set it up (a follow-up's UI, which
        # should debounce it ~10 s): "ok", "missing", "names_hidden", or "unknown"
        # when the frame isn't the game (login, loading, Cash Shop).
        self.minimap_status = "unknown"
        self.hwnd: int | None = None
        self._price_request: tuple[int, int] | None = None

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
            self._on_frame(frame, control)

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

    def _on_frame(self, frame, control) -> None:
        """Runs on windows-capture's native thread: never let an error escape, log it (throttled)."""
        try:
            if not self._running:
                control.stop()
                return
            buf = frame.frame_buffer  # BGRA
            with self._lock:
                self._frame = np.ascontiguousarray(buf[:, :, :3])
        except Exception:
            now = time.monotonic()
            if now - getattr(self, "_last_frame_error", -FRAME_ERROR_LOG_EVERY_S) >= FRAME_ERROR_LOG_EVERY_S:
                self._last_frame_error = now
                log.exception("frame handler failed (repeats are not logged for %.0fs)", FRAME_ERROR_LOG_EVERY_S)

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
            log.exception("reader crashed")
            self.info.emit(f"Reader crashed: {e}")

    def _run(self) -> None:
        reader = Reader()
        slots: QuickslotReader | None = None  # made on first use
        next_msg = next_full = next_status = next_map = next_slots = next_chat = 0.0
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
                if now >= next_full:
                    next_full = now + MESSAGES_FULL_EVERY_S
                    reader.read_messages(frame, s)
                else:
                    reader.read_messages_fast(frame, s)
                for line in s.messages:  # warm item-id cache off the UI thread
                    m = parse_message(line.text)
                    if m and m.kind == "loot":
                        gamedata.item_id(m.item)
                self.messages_read.emit(s.messages)
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                read_status(reader, frame, s)
                if s.exp is not None:  # unreadable level digits pass through as None
                    self._exp_seen_at = now
                    self.status_read.emit(s.level or None, s.exp, s.exp_percent)
            if now >= next_slots:
                next_slots = now + QUICKSLOTS_EVERY_S
                if slots is None:
                    slots = QuickslotReader(reader.ocr)
                reads = slots.read(frame)
                if reads:
                    self.quickslots_read.emit(reads)
            if now >= next_chat:
                next_chat = now + CHAT_EVERY_S
                if reader.read_chat(frame, s):  # skipped if the text didn't change
                    self.chat_read.emit(s.chat)
            if now >= next_map:
                next_map = now + MAP_EVERY_S
                reader.read_map(frame, s)
                reader.read_party(frame, s)
                self.party_read.emit(s.party)
                place = self._resolve_map(s.street or "", s.map_name or "", s.map_clipped, now)
                s.minimap_status = self.minimap_status = self._refine_minimap_status(
                    s.minimap_status, place, now)
            time.sleep(0.02)

    def _lookup_map(self, street: str, name: str, clipped: bool) -> tuple[dict | None, str | None, tuple]:
        """gamedata.match_map plus the map's mobs, cached for an unchanged read."""
        key = (street, name, clipped)
        if key != self._lookup_key:
            m, place = gamedata.match_map(street, name, clipped)
            self._lookup = (m, place, tuple(gamedata.map_mobs(m["id"])) if m else ())
            self._lookup_key = key
        return self._lookup

    def _resolve_map(self, street: str, name: str, clipped: bool = False,
                     now: float | None = None) -> str | None:
        """Turn a minimap read into ``map_read``; returns the matched place, if any.

        A resolved map shows with its mobs. A known place whose numeral didn't
        resolve (VI or VII?) shows as the raw read, marked, with no mobs, rather
        than a wrong map's mob list (#26). A change must hold MAP_CONFIRM_READS
        reads or MAP_CONFIRM_S, except the first map and a resolved read replacing
        a raw one of the same place. A raw read of the shown map's place, and a
        read that matches nothing, keep the shown map.
        """
        if not name:
            return None
        now = time.monotonic() if now is None else now
        try:
            m, place, mobs = self._lookup_map(street, name, clipped)
        except Exception as e:
            self.info.emit(f"Map lookup failed: {e}")
            return None
        if place is None:
            return None  # an unreadable frame: keep the shown map, and any pending one
        if m is None:
            display = (f"{street} : {name}" if street else name) + RAW_MAP_MARK
        else:
            display = f"{m['streetName']} : {m['name']}"
        shown = self._map_shown
        if shown is not None and (display == shown[0] or (m is None and place == shown[1])):
            self._map_pending = None
            return place
        immediate = shown is None or (m is not None and not shown[2] and place == shown[1])
        if not immediate:
            if self._map_pending is None or self._map_pending[0] != display:
                self._map_pending = (display, now, 1)
                return place
            _, first, reads = self._map_pending
            reads += 1
            self._map_pending = (display, first, reads)
            if reads < MAP_CONFIRM_READS and now - first < MAP_CONFIRM_S:
                return place
        self._map_pending = None
        self._map_shown = (display, place, m is not None)
        self.map_read.emit(display, mobs)
        return place

    def _refine_minimap_status(self, panel: str | None, place: str | None, now: float) -> str:
        """The reader's panel status, corrected with what only the worker knows."""
        if self._exp_seen_at is None or now - self._exp_seen_at > PLAYFIELD_S:
            return "unknown"  # login, loading or Cash Shop: no EXP bar, no minimap expected
        if panel == "missing" and place is not None:
            return "ok"  # another skin: the static fallback still reads the map
        return panel or "unknown"

    def _price_check(self, reader: Reader, frame, cursor: tuple[int, int]) -> None:
        from .vision.tooltip import hovered_item_names

        self.info.emit("Reading item tooltip...")
        items = hovered_item_names(reader, frame, cursor, gamedata.item_id)
        if not items:
            self.info.emit("No item tooltip found under the cursor")
        self.price_items.emit(items)
