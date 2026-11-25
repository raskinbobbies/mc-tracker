"""Shared Qt lifecycle for the whole test session.

Tests build widgets (dialogs, overlays, toasts, window-memory filters, local servers...) and mostly leave
them to the garbage collector. Many of those objects sit in reference cycles (a lambda connected to a signal
captures ``self``), so *when* they are freed is up to the interpreter. If that happens during interpreter
shutdown, after Qt has already torn down the QApplication, Qt aborts (``QPixmap: Must construct a
QGuiApplication before a QPixmap``, a QThread/QLocalServer destroyed too late...) and ``python -m pytest``
exits non-zero after printing "N passed". Which objects are freed first varies run to run, hence the flake.

So: one QApplication owned here for the whole session, and a teardown that frees every Qt object, in order,
while the application is still alive, then destroys the application itself, before Python starts to exit.
"""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _drain(app, rounds: int = 3) -> None:
    from PySide6.QtCore import QEvent

    for _ in range(rounds):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()


@pytest.fixture(scope="session", autouse=True)
def _qt_app():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # Qt-less environment: the Qt tests skip themselves
        yield None
        return
    app = QApplication.instance() or QApplication([])
    yield app
    from PySide6.QtCore import QThreadPool

    # Stop anything still running: pool workers, then top-level windows, popups and their children.
    QThreadPool.globalInstance().waitForDone(10_000)
    gc.collect()
    for w in app.topLevelWidgets():
        try:
            w.close()
            w.deleteLater()
        except RuntimeError:  # already deleted on the C++ side
            pass
    _drain(app)
    # Anything still alive in Python was freed by the passes above; now take the application down
    # while the interpreter can still run destructors.
    app.shutdown()
