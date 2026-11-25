import faulthandler
import logging
import sys
import threading

from mctracker import crash, paths


def test_ensure_std_streams_replaces_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    crash.ensure_std_streams()
    print("hello")  # must not raise
    sys.stderr.write("x")


def test_setup_logging_writes_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MC_TRACKER_HOME", str(tmp_path))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = crash.setup_logging()
        assert path == tmp_path / "logs" / "mc-tracker.log"
        logging.getLogger("mctracker.x").info("hi")
        for h in root.handlers:
            h.flush()
        assert "hi" in path.read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        faulthandler.disable()  # it points at the file we are about to close
        if crash._fault_file:
            crash._fault_file.close()


def test_frozen_home_never_inside_install_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MC_TRACKER_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths, "FROZEN_DEFAULT_HOME", tmp_path / "Maple" / ".mc-tracker")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.home() == tmp_path / "local" / "mc-tracker"  # parent folder missing -> LOCALAPPDATA
    (tmp_path / "Maple").mkdir()
    assert paths.home() == tmp_path / "Maple" / ".mc-tracker"


def test_reader_crash_is_logged(caplog, monkeypatch):
    from mctracker.capture import CaptureWorker

    w = CaptureWorker("x")
    monkeypatch.setattr(w, "_run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.ERROR, logger="mctracker.capture"):
        w._loop()
    assert any("reader crashed" in r.message and r.exc_info for r in caplog.records)


def test_frame_handler_errors_are_logged_once(caplog):
    from mctracker.capture import CaptureWorker

    w = CaptureWorker("x")
    w._running = True

    class BadFrame:
        @property
        def frame_buffer(self):
            raise ValueError("bad frame")

    with caplog.at_level(logging.ERROR, logger="mctracker.capture"):
        for _ in range(50):
            w._on_frame(BadFrame(), None)  # must not raise
    assert len([r for r in caplog.records if "frame handler failed" in r.message]) == 1


def test_thread_exceptions_are_logged(caplog, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    shown = []

    class FakeReporter:  # the real one would queue a modal box that a later test's event loop would run
        class report:
            emit = staticmethod(shown.append)

    monkeypatch.setattr(crash, "_make_reporter", FakeReporter)
    monkeypatch.setattr(crash, "_reporter", None)
    crash.install_excepthook(None)

    def boom():
        raise RuntimeError("thread boom")

    with caplog.at_level(logging.ERROR, logger="mctracker.crash"):
        t = threading.Thread(target=boom)
        t.start()
        t.join()
    assert any("thread boom" in r.getMessage() for r in caplog.records)
    assert shown and "thread boom" in shown[0]
