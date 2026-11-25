import logging
import sys

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
        crash._fault_file and crash._fault_file.close()


def test_frozen_home_never_inside_install_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("MC_TRACKER_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths, "FROZEN_DEFAULT_HOME", tmp_path / "Maple" / ".mc-tracker")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert paths.home() == tmp_path / "local" / "mc-tracker"  # parent folder missing -> LOCALAPPDATA
    (tmp_path / "Maple").mkdir()
    assert paths.home() == tmp_path / "Maple" / ".mc-tracker"
