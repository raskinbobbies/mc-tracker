"""Start and stop with MapleStory.

With the setting on, a small *waiter* (``mc-tracker --follow-game``) is registered under the
per-user login Run key. It has no window, only a tray icon, and polls for the game's window every
few seconds. When the game appears it starts the normal app as a separate process; the app itself
quits (saving its session as on any quit) when the game goes away. Keeping the waiter separate means
nothing heavy (OCR models, capture, hotkey) sits in memory between games.

Pieces, each testable on its own:

* ``GameWatcher``: the absent -> present -> absent state machine (probe and clock injected).
* ``enable_login_entry`` / ``disable_login_entry``: the HKCU Run value (``winreg`` injected).
* ``SingleInstance``: one app and one waiter per data folder, over a local socket.
* ``run_waiter``: the hidden waiter process.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import paths

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "mc-tracker"
FOLLOW_ARG = "--follow-game"
POLL_S = 3.0
MISSES_TO_STOP = 2  # consecutive polls without the game before it counts as closed (a blip must not quit the app)

# --- the state machine ----------------------------------------------------------


class GameWatcher:
    """Turns "is the game window there?" polls into ``"started"`` / ``"stopped"`` events.

    ``poll()`` may be called as often as you like: it only probes once per ``interval`` of
    ``clock``. ``"stopped"`` needs ``misses_to_stop`` probes in a row without the game.
    """

    def __init__(self, probe: Callable[[], bool], clock: Callable[[], float] = time.monotonic,
                 interval: float = POLL_S, misses_to_stop: int = MISSES_TO_STOP):
        self.probe = probe
        self.clock = clock
        self.interval = interval
        self.misses_to_stop = misses_to_stop
        self.present = False
        self._misses = 0
        self._next_at: float | None = None

    def poll(self) -> str | None:
        now = self.clock()
        if self._next_at is not None and now < self._next_at:
            return None
        self._next_at = now + self.interval
        try:
            seen = bool(self.probe())
        except Exception:  # a failed probe says nothing about the game: keep the state
            log.exception("game probe failed")
            return None
        if seen:
            self._misses = 0
            if not self.present:
                self.present = True
                return "started"
            return None
        if self.present:
            self._misses += 1
            if self._misses >= self.misses_to_stop:
                self.present = False
                self._misses = 0
                return "stopped"
        return None


def game_probe(window_title: str = "MapleStory") -> Callable[[], bool]:
    """Whether the game window exists, found the way the capture finds it."""
    from .winutil import find_game_window

    return lambda: find_game_window(title=window_title) is not None


# --- the login entry ------------------------------------------------------------


def _pythonw() -> str:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def app_command(extra: tuple[str, ...] = ()) -> list[str]:
    """The command line that starts mc-tracker: the exe when frozen, ``pythonw -m mctracker`` otherwise."""
    if paths.frozen():
        return [sys.executable, *extra]
    return [_pythonw(), "-m", "mctracker", *extra]


def run_command() -> str:
    """The Run-key value: the waiter's command line, each part quoted."""
    return subprocess.list2cmdline(app_command((FOLLOW_ARG,)))


def _winreg():
    import winreg

    return winreg


def enable_login_entry(command: str | None = None, reg=None) -> None:
    """Start the waiter at login (HKCU, no admin). Raises ``OSError`` if the registry refuses."""
    reg = reg or _winreg()
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, command or run_command())


def disable_login_entry(reg=None) -> None:
    """Remove the login entry; fine if it was never there."""
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def login_entry_present(reg=None) -> bool:
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_QUERY_VALUE) as key:
            reg.QueryValueEx(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return False


# --- processes ------------------------------------------------------------------


def _spawn(cmd: list[str]) -> bool:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        log.exception("couldn't start %s", cmd)
        return False


def spawn_app(extra: tuple[str, ...] = ()) -> bool:
    """Start the normal app, detached. It exits at once if one is already running."""
    return _spawn(app_command(extra))


def spawn_waiter() -> bool:
    """Start the waiter, detached. It exits at once if one is already running."""
    return _spawn(app_command((FOLLOW_ARG,)))


# --- one app, one waiter ---------------------------------------------------------


def instance_name(role: str) -> str:
    """Local-socket name for ``role`` ("app" / "waiter"), per user and per data folder (so a dev
    checkout and the packaged exe, which keep separate data, don't block each other)."""
    import getpass

    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    tag = hashlib.sha1(str(paths.home()).lower().encode()).hexdigest()[:10]
    return f"mc-tracker-{role}-{user}-{tag}"


class SingleInstance(QObject):
    """The first process to ``acquire`` a name owns it; later ones hand it a message and exit.

    Messages arrive on the owner as ``message(str)``.
    """

    message = Signal(str)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self._server: QLocalServer | None = None

    def acquire(self, send: str = "") -> bool:
        """True: we own the name. False: another process does, and got ``send``."""
        if self.send(self.name, send):
            return False
        QLocalServer.removeServer(self.name)  # a dead owner's leftover
        server = QLocalServer(self)
        if not server.listen(self.name):
            log.warning("single-instance listen failed: %s", server.errorString())
            return True  # can't tell: better a second window than none
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    @staticmethod
    def send(name: str, text: str = "") -> bool:
        """Deliver ``text`` to whoever owns ``name``; False if nobody does."""
        sock = QLocalSocket()
        sock.connectToServer(name)
        if not sock.waitForConnected(500):
            return False
        sock.write(text.encode("utf-8"))
        sock.waitForBytesWritten(500)
        sock.waitForReadyRead(1000)  # the owner's "ok": closing earlier can lose the message
        sock.disconnectFromServer()
        return True

    def _on_connection(self) -> None:
        server = self._server
        while server is not None and (sock := server.nextPendingConnection()) is not None:
            sock.readyRead.connect(lambda sock=sock: self._on_data(sock))
            if sock.bytesAvailable():
                self._on_data(sock)

    def _on_data(self, sock: QLocalSocket) -> None:
        text = bytes(sock.readAll()).decode("utf-8", "replace")
        sock.write(b"ok")
        sock.flush()
        sock.disconnectFromServer()
        sock.deleteLater()
        self.message.emit(text)


def ask_waiter_to_quit() -> None:
    SingleInstance.send(instance_name("waiter"), "quit")


# --- the hidden waiter ---------------------------------------------------------


def run_waiter(app, icon, window_title: str, forward: tuple[str, ...] = ()) -> int:
    """No windows: a tray icon, and a poll that starts the app whenever the game appears.

    ``app`` is the QApplication, ``icon`` its QIcon, ``forward`` extra args for the app it starts.
    """
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    inst = SingleInstance(instance_name("waiter"))
    if not inst.acquire():
        return 0
    app.setQuitOnLastWindowClosed(False)

    def open_now() -> None:
        spawn_app(forward)

    inst.message.connect(lambda text: app.quit() if text == "quit" else open_now())

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(icon)
        tray.setToolTip("mc-tracker: waiting for MapleStory")
        menu = QMenu()
        menu.addAction("Open now", open_now)
        menu.addAction("Quit", app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda r: open_now() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        tray.show()
        app._tray_menu = menu  # keep alive

    watcher = GameWatcher(game_probe(window_title))
    timer = QTimer(app)
    timer.setInterval(int(POLL_S * 1000))
    timer.timeout.connect(lambda: open_now() if watcher.poll() == "started" else None)
    timer.start()
    app._waiter = (inst, tray, timer)  # keep alive
    return app.exec()


class GameFollower(QObject):
    """Inside the running app: polls the game and reports ``started`` / ``stopped`` (see ``GameWatcher``)."""

    started = Signal()
    stopped = Signal()

    def __init__(self, probe: Callable[[], bool], parent=None, interval: float = POLL_S):
        super().__init__(parent)
        self.watcher = GameWatcher(probe, interval=interval)
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval * 1000))
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.watcher = GameWatcher(self.watcher.probe, interval=self.watcher.interval)  # fresh: absent
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        event = self.watcher.poll()
        if event == "started":
            self.started.emit()
        elif event == "stopped":
            self.stopped.emit()
