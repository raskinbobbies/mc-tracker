"""The minimap prompt's debounce: shows after a bad status holds, clears at once."""

import pytest

from mctracker.minimap_prompt import HOLD_S, MESSAGES, MinimapPrompt


@pytest.mark.parametrize("bad", ["missing", "names_hidden"])
def test_prompt_waits_for_the_hold_then_shows(bad):
    p = MinimapPrompt()
    assert p.update(bad, 100.0) is None
    assert p.update(bad, 100.0 + HOLD_S - 0.1) is None
    assert p.update(bad, 100.0 + HOLD_S) == MESSAGES[bad]
    assert p.update(bad, 500.0) == MESSAGES[bad]  # stays while the status holds


@pytest.mark.parametrize("clear", ["ok", "unknown"])
def test_prompt_clears_immediately(clear):
    p = MinimapPrompt()
    p.update("missing", 0.0)
    assert p.update("missing", 20.0) is not None
    assert p.update(clear, 20.1) is None


def test_a_blip_of_ok_restarts_the_hold():
    p = MinimapPrompt()
    p.update("missing", 0.0)
    p.update("ok", 6.0)
    assert p.update("missing", 7.0) is None
    assert p.update("missing", 16.0) is None  # only 9 s since it came back
    assert p.update("missing", 17.0) == MESSAGES["missing"]


def test_unknown_shows_nothing_and_resets_the_hold():
    p = MinimapPrompt()
    assert p.update("unknown", 0.0) is None and p.update("unknown", 60.0) is None
    p.update("names_hidden", 61.0)
    p.update("unknown", 65.0)  # loading screen
    assert p.update("names_hidden", 66.0) is None
    assert p.update("names_hidden", 75.0) is None
    assert p.update("names_hidden", 76.0) == MESSAGES["names_hidden"]


def test_switching_between_bad_statuses_restarts_the_hold():
    p = MinimapPrompt()
    p.update("missing", 0.0)
    assert p.update("names_hidden", 9.0) is None
    assert p.update("names_hidden", 18.0) is None
    assert p.update("names_hidden", 19.0) == MESSAGES["names_hidden"]


def test_relay_feeds_every_window_and_clears_them():
    from mctracker.minimap_prompt import MinimapPromptRelay

    now = [0.0]
    a, b = [], []
    relay = MinimapPromptRelay([a.append, b.append], lambda: now[0])
    relay.on_status("missing")
    now[0] = HOLD_S
    relay.on_status("missing")
    relay.on_status("ok")
    assert a == b == [None, MESSAGES["missing"], None]


def test_app_wiring_status_to_both_windows_and_cleared_by_a_saved_session(monkeypatch, tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from mctracker import capture
    from mctracker.app import wire_minimap_prompt
    from mctracker.items import AppSettings
    from mctracker.session import Tracker

    app = QApplication.instance() or QApplication([])
    now = [0.0]
    shown = {"main": [], "overlay": []}

    class Win:
        def __init__(self, key):
            self.key = key

        def set_minimap_prompt(self, text):
            shown[self.key].append(text)

    tr = Tracker(save_dir=tmp_path / "s", settings=AppSettings())
    cap = capture.CaptureWorker()
    wire_minimap_prompt(cap, tr, [Win("main"), Win("overlay")], lambda: now[0])
    cap.minimap_changed.emit("names_hidden")
    now[0] = HOLD_S
    cap.minimap_changed.emit("names_hidden")
    assert shown["main"] == shown["overlay"] == [None, MESSAGES["names_hidden"]]
    cap.minimap_changed.emit("unknown")  # capture stopped / frame lost
    assert shown["main"][-1] is None and shown["overlay"][-1] is None
    now[0] = 30.0
    cap.minimap_changed.emit("names_hidden")
    now[0] = 30.0 + HOLD_S
    cap.minimap_changed.emit("names_hidden")
    assert shown["main"][-1] == MESSAGES["names_hidden"]
    for cb in tr.on_saved:  # a session was saved
        cb(tmp_path / "x.json")
    app.processEvents()
    assert shown["main"][-1] is None and shown["overlay"][-1] is None


def test_capture_reports_unknown_when_frames_stop_or_capture_stops(monkeypatch):
    from mctracker import capture

    w = capture.CaptureWorker()
    got = []
    w.minimap_changed.connect(got.append)
    w.minimap_status = "missing"
    w._frame_at = 100.0
    assert w._minimap_status_to_show(100.0 + capture.FRAME_STALE_S) == "missing"
    assert w._minimap_status_to_show(100.1 + capture.FRAME_STALE_S) == "unknown"
    w.stop()
    assert got == ["unknown"]
