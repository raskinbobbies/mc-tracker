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
        else:
            self.info.emit(WAITING_TEXT)

    def stop(self):
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
    cap.running = True  # it works at last: the delay resets
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


def test_the_timer_drives_the_ticks(qapp):
    game = {"up": False}
    w, _cap, _ = watcher(qapp, game)
    w.start()
    assert w._timer.isActive() and w._timer.interval() == 3000
    w.stop()
    assert not w._timer.isActive()


def test_worker_reports_attached_changes(qapp):
    cap = CaptureWorker("MapleStory")
    seen = []
    cap.attached_changed.connect(seen.append)
    cap._set_attached(True)
    cap._set_attached(True)
    cap.stop()
    cap.stop()
    assert seen == [True, False]
