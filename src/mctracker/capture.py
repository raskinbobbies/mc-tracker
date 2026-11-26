"""Live capture of the game window -> GameState updates (on a worker thread)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from . import gamedata
from .kills import parse_message
from .vision.channel import StableRead, read_channel
from .vision.quickslots import QuickslotReader
from .vision.reader import GameState, Reader, crop, parse_level, parse_status_exp

log = logging.getLogger(__name__)
FRAME_STALE_S = 5.0  # no new frame for this long: the window is gone or minimised, the minimap status is stale
FRAME_ERROR_LOG_EVERY_S = 30.0  # a repeating per-frame error: log once per this many seconds

WINDOW_NAME = "MapleStory"
MESSAGES_EVERY_S = 0.15  # newest rows, recognition only (``read_messages_fast``)
MESSAGES_FULL_EVERY_S = 3.0  # with text detection: re-measures the rows, a fallback
STATUS_EVERY_S = 1.0
MAP_EVERY_S = 0.4  # minimap title (OCR only when its pixels change)
PARTY_EVERY_S = 3.0
CHANNEL_EVERY_S = 1.0  # the Change Channel dialog (almost free while it is closed)
# Map changes are voted: the last MAP_VOTES title reads are kept, keyed on
# (place, numeral), and a different map (or a raw, unresolved read) leads once it
# has MAP_CONFIRM_READS of them, so an isolated OCR slip never wins. It must lead
# for MAP_CONFIRM_S before it replaces the shown map: a misread held on screen (a
# window over the title's end) repeats the same pixels, so its reads aren't
# independent. Two faster ways in: after a loading transition (LOADING_S with no
# minimap panel and no EXP read) the next resolved read confirms at once, and an
# exact read (gamedata.exact_read: no fuzzy match, no lookalike letters, no repaired
# numeral) of another map confirms after MAP_FAST_READS in a row, MAP_FAST_WINDOW_S
# apart at most, spanning at least MAP_FAST_SPAN_S (a flip ends the session, so two
# back-to-back reads of one stuck frame must not do it). A read whose numeral is a prefix of the shown map's ("VIII" ->
# "VII": a window over the title's end) is never fast, whatever its quality.
MAP_VOTES = 5
MAP_CONFIRM_READS = 3
MAP_CONFIRM_S = 2.0
MAP_FAST_READS = 2
MAP_FAST_WINDOW_S = 1.5
MAP_FAST_SPAN_S = 0.8
MAP_VOTE_TTL_S = 8.0  # a vote older than this is dropped: reads that far apart aren't one lead
LOADING_S = 0.6
UNRESOLVED_S = 3.0  # after a loading gap, a map still unread this long: the channel is stale
RAW_MAP_MARK = " (?)"  # suffix on a raw read shown with no mobs: never equal to a resolved name
PLAYFIELD_S = 3.0  # an EXP read this recent means the frame is the game, not a login/loading screen
CHAT_EVERY_S = 5.0  # chat box: only for party join/leave lines (a line stays in the 3-line strip until 3 newer chat lines push it out)
QUICKSLOTS_EVERY_S = 1.0
WATCH_S = 3.0  # how often the game window is looked for while none is attached
WATCH_RETRY_S = 3.0  # a start that fails although the window exists is tried again after this; doubles up to the max
WATCH_RETRY_MAX_S = 60.0
WAITING_TEXT = "Waiting for MapleStory…"


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
    # map display name, tuple[Mob, ...], refine (the same visit's raw read resolved:
    # keep the session)
    map_read = Signal(str, object, bool)
    messages_read = Signal(object)  # list[TextLine]
    party_read = Signal(int)  # party members (HP-bar rows); 0 = no party widget
    chat_read = Signal(object)  # list[TextLine] in the chat box
    quickslots_read = Signal(object)  # list[SlotRead], one per quickslot
    price_items = Signal(object)  # list[(name, item_id)], hovered first
    channel_read = Signal(object)  # current channel (int), or None once the map reloaded
    minimap_changed = Signal(str)  # "ok" / "missing" / "names_hidden" / "unknown", after each map read
    info = Signal(str)
    attached_changed = Signal(bool)  # True once a game window is being read, False when it is not

    def __init__(self, window_name: str = WINDOW_NAME, parent=None):
        super().__init__(parent)
        self.window_name = window_name
        self._frame: np.ndarray | None = None
        self._frame_at = 0.0  # monotonic time the latest frame arrived
        self._lock = threading.Lock()
        self._running = False
        self._closed = False  # the game window went away under a running capture
        self._lookup_key: tuple[str, str, bool] | None = None
        self._lookup: tuple[dict | None, str | None, tuple] = (None, None, ())
        self._map_shown: tuple[str, str, bool] | None = None  # (display name, place, resolved)
        self._map_votes: deque[tuple] = deque(maxlen=MAP_VOTES)  # vote keys, newest last
        self._map_vote_at: deque[float] = deque(maxlen=MAP_VOTES)  # when each vote was cast
        self._map_fast: tuple[tuple, int, float, float] | None = None  # (key, run of exact reads, first at, last at)
        self._map_cands: dict[tuple, tuple[str, str, bool, tuple]] = {}  # key -> (display, place, resolved, mobs)
        self._map_lead: tuple[tuple, float] | None = None  # (key leading the vote, since)
        self._map_votes_key: tuple | None = None  # the shown map's vote key
        self._map_gap_since: float | None = None  # the panel has been gone since then
        self._exp_seen_at: float | None = None
        self._channel_stable = StableRead(2)  # a channel counts after two identical reads
        self._gap_loading = False  # the panel gap being timed looked like a loading screen
        self._unresolved_since: float | None = None  # the panel is back but the map still unread
        # The minimap's state, for the prompt to set it up (see ``minimap_prompt``):
        # "ok", "missing", "names_hidden", or "unknown" when the frame isn't the game
        # (login, loading, Cash Shop).
        self.minimap_status = "unknown"
        self.hwnd: int | None = None
        self._attached = False
        self._gen = 0  # bumped by every start and stop: a reader loop or callback from an older capture stands down
        self._price_request: tuple[int, int] | None = None

    # --- capture --------------------------------------------------------------

    @property
    def running(self) -> bool:
        """Capturing a live game window: False before ``start``, after a failed start or ``stop``, or once the window closed."""
        return self._running and not self._closed

    def _set_attached(self, on: bool) -> None:
        if on != self._attached:
            self._attached = on
            self.attached_changed.emit(on)

    def _live(self, gen: int) -> bool:
        """Whether the capture started as generation ``gen`` is still the one running, with its window open."""
        return self._gen == gen and self._running and not self._closed

    def start(self) -> None:
        """Attach to the game window, if there is one (``CaptureWatcher`` keeps looking while there isn't)."""
        from windows_capture import WindowsCapture

        from .winutil import find_game_window

        hwnd = find_game_window(title=self.window_name)
        if hwnd is None:
            return
        self._gen += 1
        gen = self._gen
        self.hwnd = hwnd
        with self._lock:
            self._frame = None  # never read the last game's final frame
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
        self._closed = False

        @cap.event
        def on_frame_arrived(frame, control):
            self._on_frame(frame, control, gen)

        @cap.event
        def on_closed():
            if gen != self._gen:  # a capture that was already replaced (or stopped)
                return
            self._closed = True
            self.info.emit("Game window closed")
            self.minimap_changed.emit("unknown")
            self._set_attached(False)

        try:
            self._control = cap.start_free_threaded()
        except Exception as e:  # window not found, etc.
            self.info.emit(f"Can't capture '{self.window_name}': {e}")
            self._running = False
            return
        self._thread = threading.Thread(target=self._loop, args=(gen,), name="reader", daemon=True)
        self._thread.start()
        self._set_attached(True)
        self.info.emit(f"Watching '{self.window_name}'")

    def _on_frame(self, frame, control, gen: int | None = None) -> None:
        """Runs on windows-capture's native thread: never let an error escape, log it (throttled)."""
        try:
            if not self._running or (gen is not None and gen != self._gen):
                control.stop()
                return
            buf = frame.frame_buffer  # BGRA
            with self._lock:
                self._frame = np.ascontiguousarray(buf[:, :, :3])
                self._frame_at = time.monotonic()
        except Exception:
            now = time.monotonic()
            if now - getattr(self, "_last_frame_error", -FRAME_ERROR_LOG_EVERY_S) >= FRAME_ERROR_LOG_EVERY_S:
                self._last_frame_error = now
                log.exception("frame handler failed (repeats are not logged for %.0fs)", FRAME_ERROR_LOG_EVERY_S)

    def stop(self, wait: bool = True) -> None:
        """Stop capturing. With ``wait``, give the reader thread a moment to exit (at quit); without, return
        at once (the GUI thread): the generation bump makes an old loop stand down whenever it next checks."""
        self._running = False
        self._gen += 1
        self.hwnd = None
        self._set_attached(False)
        self.minimap_changed.emit("unknown")  # nothing is being read any more: drop any prompt
        control = getattr(self, "_control", None)
        if control is not None:
            try:
                control.stop()
            except Exception:
                pass
        thread = getattr(self, "_thread", None)
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    def request_price_check(self) -> None:
        from .hotkey import cursor_in_window
        from .winutil import window_exists

        hwnd = self.hwnd
        if not self.running or hwnd is None or not window_exists(hwnd):  # closed, maybe not yet noticed
            self.info.emit("MapleStory isn't open: nothing to price-check")
            return
        self._price_request = cursor_in_window(hwnd) or (0, 0)

    # --- processing -----------------------------------------------------------

    def _latest(self) -> np.ndarray | None:
        with self._lock:
            return self._frame

    def _loop(self, gen: int = 0) -> None:
        try:
            self._run(gen)
        except Exception as e:  # surface crashes instead of dying silently
            log.exception("reader crashed")
            self.info.emit(f"Reader crashed: {e}")

    def _run(self, gen: int = 0) -> None:
        reader = Reader()
        slots: QuickslotReader | None = None  # made on first use
        next_msg = next_full = next_status = next_map = next_party = next_slots = next_chat = next_channel = 0.0
        while self._live(gen):  # not once the window closed (its last frame is frozen) or a newer capture took over
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
                if not self._live(gen):  # a slow read outlived its capture: its results are stale
                    break
                self.messages_read.emit(s.messages)
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                read_status(reader, frame, s)
                if not self._live(gen):
                    break
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
                place = self._resolve_map(s.street or "", s.map_name or "", s.map_clipped, now,
                                          panel_ok=s.minimap_status == "ok")
                s.minimap_status = self.minimap_status = self._refine_minimap_status(
                    s.minimap_status, place, now)
                self.minimap_changed.emit(self._minimap_status_to_show(now))
            if now >= next_channel:
                next_channel = now + CHANNEL_EVERY_S
                self._read_channel(frame)
            if now >= next_party:
                next_party = now + PARTY_EVERY_S
                reader.read_party(frame, s)
                self.party_read.emit(s.party)
            time.sleep(0.02)

    def _read_channel(self, frame) -> None:
        """Emit the channel on every read the dialog has shown twice running.

        The tracker is the one place that remembers it (and drops repeats), so a clear on its side
        never leaves this worker thinking the channel was already sent.
        """
        channel = self._channel_stable.update(read_channel(frame))
        if channel is not None:
            self.channel_read.emit(channel)

    def _channel_reloaded(self) -> None:
        """A loading transition back onto the same map (a channel change): the channel read is stale."""
        self._channel_stable.update(None)
        self.channel_read.emit(None)

    def _channel_unresolved(self, now: float, panel_ok: bool) -> None:
        """A loading gap that ends on a map we can't read: drop the channel after UNRESOLVED_S.

        Nothing resolves, so the same-map check can't run; show nothing rather than a stale channel.
        A gap with EXP still reading is a collapsed minimap, not loading, and keeps it.
        """
        gap = self._map_gap_since
        if gap is None:
            return
        if not panel_ok:
            if now - gap >= LOADING_S and (self._exp_seen_at is None or self._exp_seen_at < gap):
                self._gap_loading = True
        elif self._gap_loading:
            if self._unresolved_since is None:
                self._unresolved_since = now
            elif now - self._unresolved_since >= UNRESOLVED_S:
                self._gap_loading, self._unresolved_since = False, None
                self._channel_reloaded()

    def _lookup_map(self, street: str, name: str, clipped: bool) -> tuple[dict | None, str | None, tuple]:
        """gamedata.match_map plus the map's mobs, cached for an unchanged read."""
        key = (street, name, clipped)
        if key != self._lookup_key:
            m, place = gamedata.match_map(street, name, clipped)
            self._lookup = (m, place, tuple(gamedata.map_mobs(m["id"])) if m else ())
            self._lookup_key = key
            if m is None:  # so a map that never resolves can be diagnosed from the log
                log.info("Minimap read %r / %r (clipped=%s): %s", street, name, clipped,
                         "no map" if place is None else "place known, numeral unresolved")
        return self._lookup

    def _resolve_map(self, street: str, name: str, clipped: bool = False,
                     now: float | None = None, panel_ok: bool = True) -> str | None:
        """Vote a minimap read into ``map_read``; returns the matched place, if any.

        A resolved map shows with its mobs. A known place whose numeral didn't
        resolve (VI or VII?) shows as the raw read, marked, with no mobs, rather
        than a wrong map's mob list (#26). The first map shows at once; a resolved
        read right after a loading transition confirms at once; any other change
        (a refine of a raw shown map included) must lead the vote for
        MAP_CONFIRM_S. A raw read of the shown map's place votes for the shown map;
        a read that matches nothing doesn't vote.
        """
        now = time.monotonic() if now is None else now
        place = m = None
        if name:
            try:
                m, place, mobs = self._lookup_map(street, name, clipped)
            except Exception as e:
                self.info.emit(f"Map lookup failed: {e}")
                return None
        if place is None:
            if not panel_ok and self._map_gap_since is None:
                self._map_gap_since = now  # no panel and no map: maybe loading
            self._channel_unresolved(now, panel_ok)
            return None
        gap, self._map_gap_since = self._map_gap_since, None
        self._gap_loading, self._unresolved_since = False, None
        loading = (gap is not None and now - gap >= LOADING_S
                   and (self._exp_seen_at is None or self._exp_seen_at < gap))
        raw_num = gamedata.split_numeral(name)[1]
        if m is None:
            display = (f"{street} : {name}" if street else name) + RAW_MAP_MARK
            key = (place, raw_num, False)
        else:
            display = f"{m['streetName']} : {m['name']}"
            key = (place, gamedata.split_numeral(m["name"])[1], True)
        shown = self._map_shown
        if loading and not (m is not None and shown is not None and shown[2] and display != shown[0]):
            # Loaded back onto the same map, or one we can't tell apart from it: a channel change
            # reloads the map, so the channel is stale. A portal to a clearly different map keeps it.
            self._channel_reloaded()
        if shown is None or (loading and m is not None and display != shown[0]):
            self._show(key, display, place, m is not None, mobs, now=now)
            return place
        shown_key = self._map_votes_key
        if self._fast_confirmed(key, m, street, name, clipped, shown, shown_key, now):
            self._show(key, display, place, True, mobs, refine=not shown[2] and place == shown[1], now=now)
            return place
        while self._map_vote_at and now - self._map_vote_at[0] > MAP_VOTE_TTL_S:
            self._map_vote_at.popleft()
            self._map_votes.popleft()
        if self._map_lead and self._map_votes.count(self._map_lead[0]) < MAP_CONFIRM_READS:
            self._map_lead = None  # its votes aged out: the hold starts over
        if m is None and place == shown[1]:
            if not shown[2]:
                # A raw read of the place already shown raw says nothing new. It must not vote
                # either: a numeral the recognizer reads about half the time would otherwise
                # keep outvoting its own resolved reads and leave the map raw for good.
                return place
            if raw_num is not None:
                return place  # a numeral the place doesn't have: no evidence for the shown map either
            key = shown_key  # the shown map, numeral unread: a vote for no change
        self._map_cands[key] = (display, place, m is not None, mobs)
        self._map_votes.append(key)
        self._map_vote_at.append(now)
        lead = next((k for k in set(self._map_votes)
                     if self._map_votes.count(k) >= MAP_CONFIRM_READS), None)
        if lead is None or lead == shown_key:
            self._map_lead = None
            return place
        if self._map_lead is None or self._map_lead[0] != lead:
            self._map_lead = (lead, now)
        if now - self._map_lead[1] < MAP_CONFIRM_S:
            return place
        d, pl, resolved, mb = self._map_cands[lead]
        self._show(lead, d, pl, resolved, mb, refine=resolved and not shown[2] and pl == shown[1], now=now)
        return place

    def _fast_confirmed(self, key: tuple, m: dict | None, street: str, name: str, clipped: bool,
                        shown: tuple, shown_key: tuple, now: float) -> bool:
        """True once MAP_FAST_READS exact reads of the same other map, MAP_FAST_SPAN_S apart overall, came in a row."""
        if (m is None or clipped or key == shown_key or not gamedata.exact_read(street, name, m)
                or (key[0] == shown[1] and key[1] and shown_key[1] and key[1] != shown_key[1]
                    and shown_key[1].startswith(key[1]))):
            self._map_fast = None
            return False
        run, first = 1, now
        if self._map_fast and self._map_fast[0] == key and now - self._map_fast[3] <= MAP_FAST_WINDOW_S:
            run, first = self._map_fast[1] + 1, self._map_fast[2]
        self._map_fast = (key, run, first, now)
        return run >= MAP_FAST_READS and now - first >= MAP_FAST_SPAN_S - 1e-9

    def _show(self, key: tuple, display: str, place: str, resolved: bool, mobs: tuple,
              refine: bool = False, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._map_shown = (display, place, resolved)
        self._map_votes_key = key
        self._map_votes.clear()
        self._map_votes.append(key)
        self._map_vote_at.clear()
        self._map_vote_at.append(now)
        self._map_cands = {key: (display, place, resolved, mobs)}
        self._map_lead = None
        self._map_fast = None
        self.map_read.emit(display, mobs, refine)

    def _minimap_status_to_show(self, now: float) -> str:
        """The status for the prompt: "unknown" once frames stop arriving (a stale frame says nothing)."""
        return "unknown" if now - self._frame_at > FRAME_STALE_S else self.minimap_status

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


class CaptureWatcher(QObject):
    """Keeps a ``CaptureWorker`` attached to the game, whenever it starts (and again after it restarts).

    A timer (no busy loop) looks for the game window every ``interval_s`` while nothing is attached;
    while the capture runs it does nothing. A start that fails although the window exists is retried less
    and less often, so it can't spam notices or keep resetting the minimap prompt.
    """

    def __init__(self, cap, probe=None, interval_s: float = WATCH_S, clock=time.monotonic, parent=None):
        super().__init__(parent)
        self.cap = cap
        self._probe = probe or self._window_exists
        self._clock = clock
        self._wait = WATCH_RETRY_S
        self._next = 0.0
        self._announced = False  # "Waiting for MapleStory" was posted for this wait
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_s * 1000))
        self._timer.timeout.connect(self.tick)

    def _window_exists(self) -> bool:
        from .winutil import find_game_window

        return find_game_window(title=self.cap.window_name) is not None

    def start(self) -> None:
        """Attach now if the game is up (else post "Waiting for MapleStory..."), then keep checking."""
        self._timer.start()
        self.tick()

    def stop(self) -> None:
        self._timer.stop()

    def tick(self) -> None:
        cap = self.cap
        if cap.running:
            self._wait, self._next, self._announced = WATCH_RETRY_S, 0.0, False
            return
        if cap.hwnd is not None:
            cap.stop(wait=False)  # tidy a capture whose window closed, without blocking the GUI thread
        if not self._probe():
            if not self._announced:
                self._announced = True
                cap.info.emit(WAITING_TEXT)
            return
        now = self._clock()
        if now < self._next:
            return
        cap.start()
        # Always back off: a capture that dies right after starting must not restart every few seconds.
        # The branch above resets the delay once a later tick finds it running.
        self._next = now + self._wait
        self._wait = min(self._wait * 2, WATCH_RETRY_MAX_S)
