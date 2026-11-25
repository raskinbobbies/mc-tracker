"""Price-check toast: message sequence, off-thread switching, no focus stealing."""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt  # noqa: E402

from mctracker.toast import PriceFeedback, Toast  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def spin(app, until, timeout=5.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert until(), "timed out"


def record(fb):
    shown = []
    orig = fb.toast.show_message
    fb.toast.show_message = lambda text, *a, **k: (shown.append(text), orig(text, *a, **k))[0]
    return shown


def test_toast_does_not_take_focus(app):
    t = Toast()
    flags = t.windowFlags()
    for f in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
              Qt.WindowType.WindowDoesNotAcceptFocus, Qt.WindowType.Tool):
        assert flags & f
    assert t.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_sequence_copied_switching_result(app):
    seen_threads = []

    def slow_switch():
        seen_threads.append(threading.get_ident())
        time.sleep(0.7)
        return "Opened mesos.watch tab"

    fb = PriceFeedback(None, switch=slow_switch)
    shown = record(fb)
    results = []
    fb.start("Korean Fan", results.append)
    assert shown == ["Copied \u201cKorean Fan\u201d"]  # painted before the slow switch returns
    spin(app, lambda: results)
    assert shown == ["Copied \u201cKorean Fan\u201d", "Switching to mesos.watch\u2026", "Opened mesos.watch tab"]
    assert results == ["Opened mesos.watch tab"]
    assert seen_threads and seen_threads[0] != threading.get_ident()  # off the GUI thread
    assert fb.toast.isVisible()


def test_fast_result_is_not_overwritten_by_switching(app):
    fb = PriceFeedback(None, switch=lambda: "Opened mesos.watch in your browser")
    shown = record(fb)
    results = []
    fb.start("Korean Fan", results.append)
    spin(app, lambda: results)
    time.sleep(0.5)  # past the "Switching..." delay
    app.processEvents()
    assert shown[-1] == "Opened mesos.watch in your browser"
    assert "Switching to mesos.watch\u2026" not in shown


def test_failure_reason_and_worker_exception(app):
    def boom():
        raise RuntimeError("no browser")

    fb = PriceFeedback(None, switch=boom)
    shown = record(fb)
    results = []
    fb.start("X Item", results.append)
    spin(app, lambda: results)
    assert shown[-1] == "Couldn't switch to mesos.watch: no browser"


def test_nothing_found_and_stale_result_ignored(app):
    fb = PriceFeedback(None, switch=lambda: (time.sleep(0.3), "Opened mesos.watch tab")[1])
    shown = record(fb)
    results = []
    fb.start("Korean Fan", results.append)
    fb.nothing_found()
    time.sleep(0.5)
    app.processEvents()
    app.processEvents()
    assert shown[-1] == "No item found under the cursor"
    assert results == []


def test_toast_fades_out(app, monkeypatch):
    import mctracker.toast as toast

    monkeypatch.setattr(toast, "SHOW_S", 0.3)
    monkeypatch.setattr(toast, "FADE_MS", 100)
    t = toast.Toast()
    t._fade_anim.setDuration(100)
    t.show_message("hi")
    assert t.isVisible()
    spin(app, lambda: not t.isVisible())
