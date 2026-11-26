"""Settings > Safe mode: persisted, off by default, applied without a restart."""

import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker import capture  # noqa: E402
from mctracker.app import apply_safe_mode  # noqa: E402
from mctracker.capture import PAUSED_TEXT, CaptureWatcher, CaptureWorker  # noqa: E402
from mctracker.items import AppSettings  # noqa: E402
from mctracker.overlay import Overlay  # noqa: E402

KEYS = ("safe_hide_overlay", "safe_slow_capture", "safe_pause_capture")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_old_settings_files_default_to_everything_normal(tmp_path):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"overlay_scale": 1.2, "follow_game": True}))
    s = AppSettings(p)
    assert [getattr(s, k) for k in KEYS] == [False, False, False]
    assert s.problem is None


def test_safe_mode_round_trips(tmp_path):
    p = tmp_path / "app.json"
    s = AppSettings(p)
    s.safe_hide_overlay = s.safe_pause_capture = True
    s.save()
    again = AppSettings(p)
    assert (again.safe_hide_overlay, again.safe_slow_capture, again.safe_pause_capture) == (True, False, True)
    assert json.loads(p.read_text())["safe_slow_capture"] is False


def test_a_bad_safe_mode_value_falls_back_to_off(tmp_path):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"safe_slow_capture": "yes"}))
    s = AppSettings(p)
    assert s.safe_slow_capture is False and "safe_slow_capture" in (s.problem or "")


class Sig:
    def __init__(self):
        self.sent = []

    def emit(self, *a):
        self.sent.append(a)


class FakeCap:
    window_name = "MapleStory"

    def __init__(self):
        self.running, self.hwnd, self.calls, self.info = False, None, [], Sig()
        self.min_frame_s = 0.0

    def start(self):
        self.calls.append("start")
        self.running, self.hwnd = True, 42

    def stop(self, wait=True):
        assert not wait
        self.calls.append("stop")
        self.running, self.hwnd = False, None

    def detach(self):
        self.calls.append("detach")


def test_pause_stops_the_capture_and_never_restarts_it_until_off(qapp):
    cap = FakeCap()
    w = CaptureWatcher(cap, probe=lambda: True, clock=lambda: 1000.0)
    w.tick()
    assert cap.running
    w.set_paused(True)
    assert not cap.running and cap.calls == ["start", "stop", "detach"]
    assert cap.info.sent[-1] == (PAUSED_TEXT,)
    for _ in range(3):
        w.tick()
    assert cap.calls.count("start") == 1
    w.set_paused(False)
    assert cap.running and cap.calls.count("start") == 2


class FlatAssets:
    def __init__(self, tmp_path):
        from PySide6.QtGui import QPixmap

        self.path = tmp_path / "sprite.png"
        QPixmap(40, 40).save(str(self.path))

    def mob_sprite(self, _id, cb):
        cb(str(self.path))

    item_icon = mob_sprite


def make(tmp_path):
    from mctracker.session import Tracker

    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    return tr, Overlay(tr, FlatAssets(tmp_path))


def test_hidden_overlay_stays_hidden_through_show(qapp, tmp_path):
    _tr, o = make(tmp_path)
    o.show()
    assert o.isVisible()
    o.set_safe_hidden(True)
    assert not o.isVisible()
    o.show()  # the show/hide hotkey, "raise windows"
    o.set_locked(True, save=False)  # the lock re-shows the window
    assert not o.isVisible()
    o.set_safe_hidden(False)
    assert o.isVisible()


def test_safe_hide_keeps_the_users_own_overlay_state(qapp, tmp_path):
    _tr, o = make(tmp_path)
    o.set_safe_hidden(False)  # no change: an unshown overlay stays unshown (no flash before restore)
    assert not o.isVisible()
    o.show()
    o.hide()  # the user hid it
    o.set_safe_hidden(True)
    o.set_safe_hidden(False)
    assert not o.isVisible()  # turning Hide off doesn't force it on
    o.show()
    o.set_safe_hidden(True)
    o.set_safe_hidden(True)
    o.hide()  # hidden by the user meanwhile (the hotkey)
    o.set_safe_hidden(False)
    assert not o.isVisible()


def test_other_toggles_never_show_a_hidden_overlay(qapp, tmp_path):
    tr, o = make(tmp_path)
    assert not o.isVisible()
    cap = FakeCap()
    watcher = SimpleNamespace(set_paused=lambda on: None)
    tr.settings.safe_slow_capture = True
    apply_safe_mode(tr.settings, o, cap, watcher)  # also what startup does, before the window is restored
    assert not o.isVisible()


def test_startup_with_hide_on_then_turned_off_shows_it(qapp, tmp_path):
    tr, o = make(tmp_path)
    o.set_safe_hidden(True)  # from settings, at startup
    o.show()  # app.py's show after WindowMemory.restore
    assert not o.isVisible()
    o.set_safe_hidden(False)
    assert o.isVisible()


def test_menu_saves_the_choice_and_applies_it(qapp, tmp_path):
    from mctracker.gui import MainWindow

    tr, o = make(tmp_path)
    w = MainWindow(tr, o, FlatAssets(tmp_path), "alt+a")
    applied = []
    w.on_safe_mode_changed = lambda: applied.append(tr.settings.safe_pause_capture)
    assert [a.text() for a in w.safe_actions.values()][2] == "Pause capture"
    w.safe_actions["safe_pause_capture"].setChecked(True)
    assert applied == [True]
    assert AppSettings(tmp_path / "app.json").safe_pause_capture is True


def test_apply_safe_mode_sets_everything_now(qapp):
    calls = []
    overlay = SimpleNamespace(set_safe_hidden=lambda on: calls.append(("overlay", on)),
                              set_safe_window=lambda off, browser: calls.append(("window", off, browser)))
    watcher = SimpleNamespace(set_paused=lambda on: calls.append(("paused", on)))
    cap = FakeCap()
    s = SimpleNamespace(safe_hide_overlay=True, safe_slow_capture=True, safe_pause_capture=False,
                        safe_overlay_off_game=True, safe_browser_overlay=False)
    apply_safe_mode(s, overlay, cap, watcher)
    assert cap.min_frame_s == capture.SLOW_FRAME_S
    assert calls == [("overlay", True), ("window", True, False), ("paused", False)]
    s.safe_slow_capture = False
    apply_safe_mode(s, overlay, cap, watcher)
    assert cap.min_frame_s == 0.0


def test_slow_capture_drops_frames_closer_than_its_interval(qapp, monkeypatch):
    import numpy as np

    cap = CaptureWorker()
    cap._running = True
    cap.min_frame_s = capture.SLOW_FRAME_S
    now = [100.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: now[0])
    frame = SimpleNamespace(frame_buffer=np.zeros((2, 2, 4), np.uint8))
    control = SimpleNamespace(stop=lambda: None)
    kept = []
    for t in (100.0, 100.15, 100.3, 100.4, 100.47, 100.65, 101.0):  # 0.47: WGC's 0.5 s with jitter
        now[0] = t
        cap._on_frame(frame, control)
        kept.append(cap._frame_at)
    assert sorted(set(kept)) == [100.0, 100.47, 101.0]
    cap.min_frame_s = 0.0  # off again: every frame is read
    now[0] = 101.15
    cap._on_frame(frame, control)
    assert cap._frame_at == 101.15


def test_each_capture_start_searches_for_the_window_afresh(qapp, monkeypatch):
    pytest.importorskip("windows_capture")
    from mctracker import winutil

    calls = []
    monkeypatch.setattr(winutil, "forget_game", lambda: calls.append("forget"))
    monkeypatch.setattr(winutil, "find_game_window", lambda **_kw: calls.append("find"))
    CaptureWorker().start()
    assert calls == ["forget", "find"]
