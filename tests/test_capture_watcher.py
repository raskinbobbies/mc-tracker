"""CaptureWatcher: the tracker finds the game by itself, whenever it starts or restarts."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.capture import WAITING_TEXT, CaptureWatcher, CaptureWorker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Signal:
    def __init__(self):
        self.sent = []

    def emit(self, *a):
        self.sent.append(a)


class FakeCap:
    """The parts of CaptureWorker the watcher uses; ``start`` attaches only while the game is up."""

    window_name = "MapleStory"

    def __init__(self, game):
        self.game = game  # dict: {"up": bool}
        self.running = False
        self.hwnd = None
        self.calls = []
        self.info = Signal()

    def start(self):
        self.calls.append("start")
        if self.game["up"]:
            self.running, self.hwnd = True, 42

    def stop(self, wait=True):
        assert not wait  # the watcher runs on the GUI thread: it must never block on the reader
        self.calls.append("stop")
        self.running, self.hwnd = False, None


def watcher(qapp, game, cap=None, now=None):
    cap = cap or FakeCap(game)
    now = now if now is not None else {"t": 1000.0}
    return CaptureWatcher(cap, probe=lambda: game["up"], clock=lambda: now["t"]), cap, now


def test_no_game_then_it_appears_and_attaches_then_closes_and_waits_again(qapp):
    game = {"up": False}
    w, cap, _now = watcher(qapp, game)
    w.tick()
    w.tick()
    assert cap.calls == []  # no window: nothing to start
    assert cap.info.sent == [(WAITING_TEXT,)]  # said once, not every tick
    game["up"] = True  # MapleStory starts
    w.tick()
    assert cap.calls == ["start"] and cap.running
    w.tick()
    assert cap.calls == ["start"]  # attached: left alone
    cap.running = False  # the window closed under the capture
    game["up"] = False
    w.tick()
    assert cap.calls == ["start", "stop"] and cap.hwnd is None  # tidied, not restarted
    assert cap.info.sent[-1] == (WAITING_TEXT,) and len(cap.info.sent) == 2
    w.tick()
    assert cap.calls == ["start", "stop"] and len(cap.info.sent) == 2
    game["up"] = True  # and again after the game restarts
    w.tick()
    assert cap.calls == ["start", "stop", "start"] and cap.running


def test_a_failing_start_is_retried_less_and_less(qapp):
    game = {"up": True}
    cap = FakeCap(game)
    cap.start = lambda: cap.calls.append("start")  # the window exists but the capture never runs
    w, cap, now = watcher(qapp, game, cap)
    counts = []
    for _ in range(40):  # two minutes of 3 s ticks
        w.tick()
        counts.append(cap.calls.count("start"))
        now["t"] += 3
    assert 3 <= counts[-1] <= 6  # tries at 0, 3, 9, 21, 45, 93 s ... not 40 of them
    cap.running = True  # it works at last, and stays up: the delay resets
    w.tick()
    now["t"] += 60
    w.tick()
    cap.running = False
    now["t"] += 3
    before = cap.calls.count("start")
    w.tick()
    assert cap.calls.count("start") == before + 1


def test_a_capture_that_dies_right_after_starting_is_not_restarted_every_tick(qapp):
    game = {"up": True}
    w, cap, now = watcher(qapp, game)
    times = []
    for _ in range(40):
        w.tick()
        if cap.calls.count("start") > len(times):
            times.append(now["t"])
        cap.running = False  # looks fine for a moment, dead before the next tick
        now["t"] += 3
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[:3] == [3, 6, 12] and len(times) <= 7


def test_start_checks_at_once_and_says_waiting_only_once(qapp):
    game = {"up": False}
    w, cap, _ = watcher(qapp, game)
    w.start()
    assert w._timer.isActive() and w._timer.interval() == 3000
    assert cap.info.sent == [(WAITING_TEXT,)]
    w.tick()
    assert cap.info.sent == [(WAITING_TEXT,)]
    w.stop()
    assert not w._timer.isActive()


def test_start_attaches_at_once_when_the_game_is_up(qapp):
    game = {"up": True}
    w, cap, _ = watcher(qapp, game)
    w.start()
    w.stop()
    assert cap.calls == ["start"] and cap.info.sent == []


def test_worker_reports_attached_changes(qapp):
    cap = CaptureWorker("MapleStory")
    seen = []
    cap.attached_changed.connect(seen.append)
    cap._set_attached(True)
    cap._set_attached(True)
    cap.stop()
    cap.stop()
    assert seen == [True, False]


# --- detach ends the session --------------------------------------------------------------------

def test_detach_saves_the_session_and_a_reattach_starts_fresh(qapp, tmp_path, monkeypatch):
    import time

    from PySide6.QtCore import QObject
    from PySide6.QtCore import Signal as QtSignal

    from mctracker.app import wire_game_detach
    from mctracker.demo import DemoFeed
    from mctracker.items import AppSettings
    from mctracker.session import Tracker

    class Cap(QObject):
        attached_changed = QtSignal(bool)

    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    cap = Cap()
    wire_game_detach(cap, tr)
    cap.attached_changed.emit(True)
    feed = DemoFeed(tr)
    feed.timer.stop()
    for _ in range(5):
        feed.tick()
        clock[0] += 1.0
    first = tr.session
    assert first is not None
    cap.attached_changed.emit(False)  # the game window closed
    assert tr.session is None and tr._status is None
    assert len(list((tmp_path / "sessions").glob("*.json"))) == 1  # saved like a normal end
    clock[0] += 600  # the game is closed for ten minutes: no session clock runs
    cap.attached_changed.emit(True)  # and reopened
    assert tr.session is None  # nothing carried over
    for _ in range(5):
        feed.tick()
        clock[0] += 1.0
    assert tr.session is not None and tr.session is not first
    assert tr.session.active_s < 60


# --- the reader thread across a restart -------------------------------------------------------------

def test_a_slow_reader_from_the_old_capture_stands_down_on_restart(qapp, monkeypatch):
    import sys
    import threading
    import time
    import types

    import numpy as np
    from PySide6.QtCore import Qt

    from mctracker import capture, winutil

    handlers = []

    class FakeWindowsCapture:
        def __init__(self, **_kw):
            self.h = {}
            handlers.append(self.h)

        def event(self, fn):
            self.h[fn.__name__] = fn
            return fn

        def start_free_threaded(self):
            return types.SimpleNamespace(stop=lambda: None)

    entered = threading.Event()

    class SlowReader:
        ocr = None

        def read_messages_fast(self, _frame, _s):
            entered.set()
            time.sleep(0.4)  # one slow OCR pass, still running when the capture restarts

        read_messages = read_messages_fast

        def read_chat(self, _frame, _s):
            return False

        def read_map(self, _frame, _s):
            pass

        def read_party(self, _frame, _s):
            pass

    monkeypatch.setitem(sys.modules, "windows_capture", types.SimpleNamespace(WindowsCapture=FakeWindowsCapture))
    monkeypatch.setattr(winutil, "find_game_window", lambda **_kw: 42)
    monkeypatch.setattr(capture, "Reader", SlowReader)
    monkeypatch.setattr(capture, "read_status", lambda *_a: None)
    monkeypatch.setattr(capture, "QuickslotReader", lambda _ocr: types.SimpleNamespace(read=lambda _f: []))
    cap = CaptureWorker("MapleStory")
    for name in ("_resolve_map", "_read_channel"):
        monkeypatch.setattr(cap, name, lambda *_a, **_k: None)
    monkeypatch.setattr(cap, "_refine_minimap_status", lambda *_a: "ok")
    monkeypatch.setattr(cap, "_minimap_status_to_show", lambda _now: "ok")
    emits = []
    # direct: record the emitting thread (a queued call would run later, on this one)
    cap.messages_read.connect(lambda _m: emits.append(threading.current_thread()), Qt.ConnectionType.DirectConnection)
    frame = np.zeros((10, 10, 3), np.uint8)

    cap.start()
    old = cap._thread
    cap._frame = frame
    assert entered.wait(2)
    cap.stop(wait=False)  # the game closed mid-read; the watcher doesn't wait
    cap.start()  # and it is back before the old read finished
    new = cap._thread
    assert new is not old
    handlers[0]["on_closed"]()  # a late close from the old capture
    assert cap.running  # ... doesn't mark the new one closed
    old.join(2)
    assert not old.is_alive()
    assert old not in emits  # the old loop's stale read was dropped, not sent
    cap._frame = frame
    deadline = time.monotonic() + 3
    while new not in emits and time.monotonic() < deadline:
        time.sleep(0.05)
    assert new in emits and new.is_alive()  # only the new loop reads
    cap.stop()
    assert not new.is_alive()


def test_the_loop_stops_reading_once_the_window_closed(qapp):
    cap = CaptureWorker("MapleStory")
    cap._running, cap._gen = True, 3
    assert cap._live(3) and not cap._live(2)
    cap._closed = True  # its last frame is frozen: don't keep reading it
    assert not cap._live(3)


def test_price_check_needs_a_live_window(qapp, monkeypatch):
    from mctracker import winutil

    cap = CaptureWorker("MapleStory")
    notes = []
    cap.info.connect(notes.append)
    cap._running, cap.hwnd = True, 42
    monkeypatch.setattr(winutil, "window_exists", lambda _h: False)  # closed, not noticed yet
    cap.request_price_check()
    assert cap._price_request is None and notes == ["MapleStory isn't open: nothing to price-check"]


def test_detach_reported_from_another_thread_runs_on_the_ui_thread(qapp, tmp_path):
    import threading

    from PySide6.QtCore import QObject
    from PySide6.QtCore import Signal as QtSignal

    from mctracker.app import wire_game_detach

    class Cap(QObject):
        attached_changed = QtSignal(bool)

    class Tr:
        threads = []

        def game_closed(self):
            self.threads.append(threading.current_thread())

    cap, tr = Cap(), Tr()
    wire_game_detach(cap, tr)
    t = threading.Thread(target=lambda: cap.attached_changed.emit(False))  # as windows-capture's on_closed
    t.start()
    t.join()
    assert tr.threads == []  # queued, not run on the capture thread
    qapp.processEvents()
    assert tr.threads == [threading.main_thread()]


def test_closing_the_game_clears_a_pause(qapp, tmp_path):
    from mctracker.items import AppSettings
    from mctracker.session import Tracker

    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    tr.pause()
    tr.game_closed()
    assert not tr.paused and tr.state != "paused"


def test_a_stale_loop_sends_nothing_and_leaves_the_price_request(qapp):
    import threading

    from PySide6.QtCore import Qt

    cap = CaptureWorker("MapleStory")
    got = []
    for sig in (cap.map_read, cap.minimap_changed, cap.channel_read, cap.party_read, cap.chat_read,
                cap.quickslots_read, cap.price_items, cap.info):
        sig.connect(lambda *a, _g=got: _g.append(a), Qt.ConnectionType.DirectConnection)
    cap._running, cap._gen = True, 2

    def old_reader():  # a reader thread of generation 1, finishing its reads after the restart
        cap._reader_gen.gen = 1
        cap._show(("k",), "Street : Old Map", "Old Map", True, ())
        cap._send(cap.minimap_changed, "missing")
        cap._channel_reloaded()
        cap._send(cap.party_read, 3)

    t = threading.Thread(target=old_reader)
    t.start()
    t.join()
    assert got == []
    cap._send(cap.party_read, 2)  # the GUI thread (or a live loop) still sends
    assert got == [(2,)]
