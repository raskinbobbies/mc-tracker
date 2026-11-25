"""Logging and crash reporting for the windowed (no console) build.

A windowed exe has no stdout/stderr, so: give them a harmless sink, log to
``<data dir>\\logs``, and show unhandled exceptions in a message box.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path

LOG_NAME = "mc-tracker.log"

_fault_file = None  # kept open for faulthandler
_dialog_open = False
_reporter = None


def ensure_std_streams() -> None:
    """Windowed builds start with sys.stdout/sys.stderr = None; make writes harmless."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def log_dir() -> Path:
    from .paths import sub

    return sub("logs")


def setup_logging() -> Path | None:
    """Log to a rotating file in the data dir. Returns the log path (None if unwritable)."""
    global _fault_file
    try:
        d = log_dir()
        path = d / LOG_NAME
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("mctracker").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:  # native crashes (access violations) leave a traceback here
        _fault_file = open(d / "fault.log", "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(_fault_file)
    except (OSError, RuntimeError):
        pass
    logging.getLogger(__name__).info("start, frozen=%s, argv=%s", getattr(sys, "frozen", False), sys.argv[1:])
    return path


def _make_reporter():
    """A QObject living in the GUI thread: any thread can emit, the box shows on the GUI thread."""
    from PySide6.QtCore import QObject, Signal

    class Reporter(QObject):
        report = Signal(str)

        def __init__(self) -> None:
            super().__init__()
            self.report.connect(self._show)

        def _show(self, text: str) -> None:
            global _dialog_open
            if _dialog_open:  # a burst of errors: one box is enough, they are all in the log
                return
            from PySide6.QtWidgets import QMessageBox

            _dialog_open = True
            try:
                box = QMessageBox(QMessageBox.Icon.Critical, "mc-tracker error",
                                  "mc-tracker hit an unexpected error. It may keep working, but consider restarting.",
                                  QMessageBox.StandardButton.Ok)
                box.setInformativeText(f"Details were written to:\n{log_dir() / LOG_NAME}")
                box.setDetailedText(text)
                box.exec()
            finally:
                _dialog_open = False

    return Reporter()


def install_excepthook(app) -> None:
    """Log unhandled exceptions (main and worker threads) and show a message box."""
    global _reporter
    _reporter = _make_reporter()
    log = logging.getLogger("mctracker.crash")
    previous = sys.excepthook

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return previous(exc_type, exc, tb)
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Unhandled exception:\n%s", text)
        try:
            _reporter.report.emit(text)
        except Exception:  # never let the reporter itself raise
            log.exception("could not show the error box")

    def thread_hook(args) -> None:
        hook(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thread_hook
