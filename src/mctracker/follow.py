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
import os
import subprocess
import sys
import tempfile
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
POLL_S = 3.0  # how often the game is looked for
TICK_S = 1.0  # timer period; the watcher itself probes once per POLL_S, so timer jitter can't skip a probe
# The game must be gone this long before it counts as closed: its window can vanish for a few
# seconds on a resolution or fullscreen switch.
STOP_GRACE_S = 15.0
MISSES_TO_STOP = round(STOP_GRACE_S / POLL_S)
SPAWN_COOLDOWN_S = 20.0  # after starting the app, give it time to come up before the waiter looks again
MAX_STARTS_PER_GAME = 3  # per game session, so an app that crashes at startup can't loop
HANDOVER_WAIT_S = 6.0  # a new waiter waits this long for the one it replaces to exit
EXE_NAME = "mc-tracker.exe"
BUILD_FILE = "build.txt"  # next to the exe, written by packaging/stamp.ps1
INSTALL_FILE = "install.txt"  # next to the exe in the fixed install folder: that folder's path (install.ps1)
NO_SWAP_ARG = "--no-swap"  # after a swap that didn't happen: don't hand over again at once
INSTALL_SCRIPT = "install.ps1"  # bundled in the exe's _internal folder
SWAP_WAIT_S = 60  # how long the install script waits for the installed exe to stop running

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
        self.probed = False  # whether the last ``poll()`` actually looked
        self._misses = 0
        self._next_at: float | None = None

    def poll(self) -> str | None:
        now = self.clock()
        self.probed = False
        if self._next_at is not None and now < self._next_at:
            return None
        self._next_at = now + self.interval
        self.probed = True
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


def run_command(extra: tuple[str, ...] = ()) -> str:
    """The Run-key value: the waiter's command line, each part quoted. ``extra``: options the
    user started the app with (a custom ``--hotkey``), kept for the apps the waiter starts."""
    return subprocess.list2cmdline(app_command((*extra, FOLLOW_ARG)))


def _winreg():
    import winreg

    return winreg


def enable_login_entry(command: str | None = None, reg=None) -> None:
    """Start the waiter at login (HKCU, no admin). Raises ``OSError`` if the registry refuses."""
    if reg is None:
        reg = _winreg()
    with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
        reg.SetValueEx(key, RUN_VALUE, 0, reg.REG_SZ, command or run_command())


def disable_login_entry(reg=None) -> None:
    """Remove the login entry; fine if it was never there."""
    if reg is None:
        reg = _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def login_entry_value(reg=None) -> str | None:
    """The command the login entry runs, or None if there is no entry."""
    if reg is None:
        reg = _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_QUERY_VALUE) as key:
            return str(reg.QueryValueEx(key, RUN_VALUE)[0])
    except FileNotFoundError:
        return None


def login_entry_present(reg=None) -> bool:
    return login_entry_value(reg) is not None


def command_target(command: str) -> Path | None:
    """The program a Run command starts, or None if it can't be told. A quoted first word is taken
    as is (``list2cmdline`` quotes any path with a space). An unquoted value may hold a path with
    spaces (an old hand-made entry, say), so the longest leading run of words that is an
    existing file wins, else the first run that ends in ``.exe`` (a program that is gone); if
    there is neither, the program is unknown."""
    s = command.strip()
    if s.startswith('"'):
        end = s.find('"', 1)
        word = s[1:end] if end > 0 else s[1:]
        return Path(word) if word else None
    words = s.split(" ")
    for n in range(len(words), 0, -1):
        candidate = Path(" ".join(words[:n]))
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            pass
    for n in range(1, len(words) + 1):
        if words[n - 1].lower().endswith(".exe"):
            return Path(" ".join(words[:n]))
    return None


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def build_stamp(exe: Path) -> str:
    """The build stamp ``build.ps1`` writes next to a packaged exe (``build.txt``: UTC time, then the
    commit), or "" for a build without one (older builds, the dev checkout). Stamps sort by age."""
    try:
        return exe.with_name(BUILD_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _is_app_exe(p: Path) -> bool:
    return p.name.lower() == EXE_NAME


def is_installed(exe: Path) -> bool:
    """Whether ``exe`` runs from the fixed install folder install.ps1 put it in (not a build output folder)."""
    try:
        recorded = exe.with_name(INSTALL_FILE).read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return False
    return bool(recorded) and _same_path(Path(recorded), exe.parent)


def check_login_entry(current: str, wanted: str, stamp: Callable[[Path], str] = build_stamp,
                      installed: Callable[[Path], bool] = is_installed) -> str:
    """What to do with the registered command ``current`` when this install would register ``wanted``:

    * ``"keep"``: it is already ``wanted``;
    * ``"rewrite"``: it starts this install's program (only the options differ, e.g. a new hotkey), is
      blank, or its program is gone from a drive that is there (a moved or deleted exe);
    * ``"takeover"``: both are packaged mc-tracker exes, ours runs from the fixed install folder
      (``installed``) and is the newer build (``stamp``): an older build folder must not keep the login
      entry once a newer build has been installed (a build output folder never takes it);
    * ``"foreign"``: anything else: another install's program, or one we can't tell is gone (an
      unreadable value, a relative path, an offline share or unmounted drive). Left alone: the dev
      checkout and the exe share the one Run value, and each taking it over on every start made it
      flip-flop. (Builds can't flip-flop: only the newer one takes over.)
    """
    if current == wanted:
        return "keep"
    ours = command_target(wanted)
    if not current.strip() or ours is None:
        return "rewrite"
    theirs = command_target(current)
    if theirs is None:
        return "foreign"
    if _same_path(theirs, ours):
        return "rewrite"
    if theirs.exists():
        if _is_app_exe(ours) and _is_app_exe(theirs) and installed(ours) and stamp(ours) > stamp(theirs):
            return "takeover"
        return "foreign"
    anchor = theirs.anchor  # the drive or share root: if that is missing too, the exe may just be offline
    return "rewrite" if anchor and Path(anchor).exists() else "foreign"


# --- processes ------------------------------------------------------------------


def _spawn(cmd: list[str]) -> bool:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        # cwd: never the exe's folder (the default from Explorer or a shortcut): a child sitting in it
        # would block the install swap from renaming it
        subprocess.Popen(cmd, creationflags=flags, close_fds=True, cwd=tempfile.gettempdir(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        log.exception("couldn't start %s", cmd)
        return False


def spawn_app(extra: tuple[str, ...] = ()) -> bool:
    """Start the normal app, detached. It exits at once if one is already running."""
    return _spawn(app_command(extra))


def spawn_waiter(extra: tuple[str, ...] = ()) -> bool:
    """Start the waiter, detached. It exits at once if one is already running."""
    return _spawn(app_command((*extra, FOLLOW_ARG)))


def spawn_command(command: str) -> bool:
    """Start a Run-key command line as it is (Windows takes the string whole), detached."""
    return _spawn(command)  # type: ignore[arg-type]


# --- the installed build and its staged update ------------------------------------
#
# build.ps1 stages each new build next to the install folder (``<install>.next``) and runs
# install.ps1, which swaps it in when the installed exe isn't running. If it is (the waiter, almost
# always), the build stays staged and the running install hands over: the waiter when the game is
# away and no app is up, the app when it is started. Either starts install.ps1 and exits; the script
# waits for the exe to be free, swaps the folders and starts the new exe again.


def staged_update(exe: Path) -> Path | None:
    """The staged build waiting to replace the folder ``exe`` runs from, or None."""
    folder = exe.parent
    staged = folder.with_name(folder.name + ".next")
    return staged if (staged / EXE_NAME).is_file() else None


def repair_install(exe: Path) -> bool:
    """``exe``'s install folder is missing (a swap cut off between its two renames): finish the swap from
    ``<folder>.next``, else put back the newest ``<folder>.old-*``. True if the folder is back."""
    folder = exe.parent
    if folder.exists() or not folder.parent.is_dir():
        return False
    try:  # install.ps1 holds this open while it swaps (then it can't be deleted): leave the swap to it
        folder.with_name(folder.name + ".lock").unlink(missing_ok=True)
    except OSError:
        return False
    staged = folder.with_name(folder.name + ".next")
    olds = sorted((p for p in folder.parent.glob(folder.name + ".old-*") if (p / EXE_NAME).is_file()), reverse=True)
    for source in ([staged] if (staged / EXE_NAME).is_file() else []) + olds[:1]:
        try:
            source.rename(folder)
        except OSError:
            log.exception("couldn't restore %s from %s", folder, source)
            continue
        log.warning("restored the missing install %s from %s", folder, source)
        return True
    return False


def _install_script(staged: Path) -> Path | None:
    for p in (staged / "_internal" / INSTALL_SCRIPT, staged / INSTALL_SCRIPT):
        if p.is_file():
            return p
    return None


def install_command(script: Path, install_dir: Path, start_args: str | None) -> list[str]:
    """The hidden PowerShell call that swaps the staged build in, then starts the installed exe with
    ``start_args`` (None: start nothing)."""
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", str(script), "-InstallDir", str(install_dir),
           "-WaitSeconds", str(SWAP_WAIT_S)]
    if start_args is not None:
        cmd += ["-Start", "-Arguments", start_args or " "]  # -File drops an empty argument
    return cmd


def start_install(exe: Path, start_args: str | None, spawn: Callable[[list[str]], object] = _spawn) -> bool:
    """Hand the swap to install.ps1 (the staged build's copy, run from a temp file so the staged folder
    can be renamed). The caller exits next: the script waits for that. False: nothing was started."""
    staged = staged_update(exe)
    script = _install_script(staged) if staged else None
    if script is None:
        return False
    try:
        fd, tmp = tempfile.mkstemp(prefix="mc-tracker-install-", suffix=".ps1")
        with os.fdopen(fd, "wb") as f:
            f.write(script.read_bytes())
    except OSError:
        log.exception("couldn't copy %s", script)
        return False
    log.info("handing over to the staged build in %s", staged)
    return bool(spawn(install_command(Path(tmp), exe.parent, start_args)))


class SwapWatch:
    """In the waiter: once a staged build is there and the game and app are both away, start the swap
    (once) and quit, so the install script can replace this folder and start the new waiter."""

    def __init__(self, exe: Path | None, start_args: str, game_present: Callable[[], bool],
                 app_up: Callable[[], bool], quit_: Callable[[], object],
                 start: Callable[[Path, str], bool] = start_install):
        self.exe = exe  # None: not a packaged build, nothing to swap
        self.start_args = start_args
        self.game_present = game_present
        self.app_up = app_up
        self.quit = quit_
        self.start = start
        self.tried = False

    def check(self) -> bool:
        """True once the swap has started and the waiter was told to quit."""
        if self.exe is None or self.tried or staged_update(self.exe) is None:
            return False
        try:
            busy = self.game_present() or self.app_up()
        except Exception:  # can't tell: not now
            log.exception("swap check failed")
            return False
        if busy:
            return False
        self.tried = True  # one try: a script that can't start shouldn't be retried every few seconds
        if not self.start(self.exe, self.start_args):
            return False
        self.quit()
        return True


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


def _create_mutex(name: str) -> tuple[int, bool]:
    """A named, per-session mutex: (handle, already existed). Handle 0 if it can't be made."""
    if sys.platform != "win32":
        return 0, False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, "Local\\" + name)
    if not handle:
        return 0, False
    return int(handle), ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def _close_handle(handle: int) -> None:
    if handle:
        import ctypes

        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(handle))


def _lock_path(name: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{name}.lock"


def _lock_fd(fd: int, lock: bool) -> None:
    """Lock or unlock the file's first byte, without waiting. Raises ``OSError`` if someone else holds it."""
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


def _take_lock(name: str) -> tuple[int | None, bool]:
    """A second, file-based claim on ``name``, for where the named mutex can't be relied on (other
    platforms, a mutex we're denied): (fd, held by another process). ``fd`` is None if no lock file can
    be used at all. The OS drops the lock with its owner, so there are no stale locks either. (The
    file is removed on a clean exit on Windows only; elsewhere it stays, empty.)"""
    try:
        fd = os.open(_lock_path(name), os.O_RDWR | os.O_CREAT)
    except OSError:
        return None, False
    for _attempt in range(3):  # a probe (``lock_held``) holds it for a moment: don't mistake it for an owner
        try:
            _lock_fd(fd, True)
            return fd, False
        except OSError:
            time.sleep(0.02)
    os.close(fd)
    return None, True


def _drop_lock(name: str, fd: int | None) -> None:
    if fd is None:
        return
    try:
        _lock_fd(fd, False)
    except OSError:
        pass
    os.close(fd)
    if sys.platform == "win32":
        try:
            _lock_path(name).unlink()  # refused while another process has it open: then it is theirs
        except OSError:
            pass
    # elsewhere the file stays: unlinking a file another process has flock'ed would let a third take a new one


def lock_held(name: str) -> bool:
    """Whether another process holds the lock file for ``name`` (see ``_take_lock``)."""
    path = _lock_path(name)
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        _lock_fd(fd, True)
    except OSError:
        return True
    else:
        _lock_fd(fd, False)
        return False
    finally:
        os.close(fd)


class SingleInstance(QObject):
    """The first process to ``acquire`` a name owns it; later ones hand it a message and exit.

    Ownership is a named mutex plus a lock file, both claimed (they die with their owner, so there are
    no stale locks) and either one enough to refuse a second process; messages travel over a local
    socket and arrive on the owner as ``message(str)``. Empty messages are dropped.
    """

    message = Signal(str)

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self._server: QLocalServer | None = None
        self._mutex = 0
        self._lock: int | None = None

    def acquire(self, send: str = "") -> bool:
        """True: we own the name. False: another process does, and got ``send`` (if it is listening yet)."""
        handle, existed = _create_mutex(self.name)
        lock = None
        if not existed:
            lock, held = _take_lock(self.name)
            existed = held
        if existed:
            _close_handle(handle)
            if send:
                self.send(self.name, send)
            return False
        owner = bool(handle or lock is not None)  # something other processes can see that we hold
        if not owner and self.send(self.name, send):  # nothing to claim the name with: ask the socket
            return False
        self._mutex, self._lock = handle, lock
        if owner:
            QLocalServer.removeServer(self.name)  # we own the name, so anything left on it is a dead owner's
        server = QLocalServer(self)
        if not server.listen(self.name):
            log.warning("single-instance listen failed: %s", server.errorString())
            if not owner:  # the name is taken by a process that didn't answer: don't start a second
                return False
            return True  # can't tell: better a second window than none
        server.newConnection.connect(self._on_connection)
        self._server = server
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        _close_handle(self._mutex)
        self._mutex = 0
        _drop_lock(self.name, self._lock)
        self._lock = None

    @staticmethod
    def send(name: str, text: str = "") -> bool:
        """Deliver ``text`` to whoever owns ``name``; True only once the owner acknowledged it."""
        sock = QLocalSocket()
        sock.connectToServer(name)
        if not sock.waitForConnected(500):
            return False
        sock.write(text.encode("utf-8"))
        sock.waitForBytesWritten(500)
        acked = sock.waitForReadyRead(1000) and bytes(sock.readAll()) == b"ok"  # closing earlier can lose the message
        sock.disconnectFromServer()
        return acked

    def _on_connection(self) -> None:
        server = self._server
        while server is not None and (sock := server.nextPendingConnection()) is not None:
            sock.readyRead.connect(lambda sock=sock: self._on_data(sock))
            if sock.bytesAvailable():
                self._on_data(sock)

    def _on_data(self, sock: QLocalSocket) -> None:
        text = bytes(sock.readAll()).decode("utf-8", "replace")
        try:
            sock.readyRead.disconnect()  # one message per connection
        except RuntimeError:
            pass
        sock.disconnected.connect(sock.deleteLater)
        sock.bytesWritten.connect(lambda _n: sock.disconnectFromServer())  # close only after the ack is out
        sock.write(b"ok")
        if text:
            self.message.emit(text)


def mutex_exists(name: str) -> bool:
    """Whether some process holds the named mutex (see ``SingleInstance``)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.OpenMutexW(0x00100000, False, "Local\\" + name)  # SYNCHRONIZE
    if not handle:
        return False
    _close_handle(int(handle))
    return True


def app_running() -> bool:
    """Whether an app instance exists. Asked of its mutex and lock file, not of its socket: a busy
    app (saving a session, stopping the capture) can't answer for a few seconds and must not be taken for gone."""
    name = instance_name("app")
    return mutex_exists(name) or lock_held(name)


def waiter_running() -> bool:
    name = instance_name("waiter")
    return mutex_exists(name) or lock_held(name)


def ask_waiter_to_quit() -> None:
    SingleInstance.send(instance_name("waiter"), "quit")


def ask_waiter_to_pause() -> None:
    """The user closed the app while the game is up: the waiter stays quiet until the game has gone."""
    SingleInstance.send(instance_name("waiter"), "pause")


# --- the hidden waiter ---------------------------------------------------------


class WaiterLogic:
    """What the waiter does on each tick: whenever the game is there and no app answers, start one.

    Deciding on the current state (not only on a "started" edge) means an app that quit while the
    game stayed up is brought back, and the waiter's and the app's polls can't get out of step on a
    quick game restart. Limits: ``cooldown`` stops a second start while the first is still coming up;
    at most ``max_starts`` per game session, so a crashing app can't loop; and after ``pause()`` (the
    user closed the app) nothing starts until the game has gone, so closing it mid-game sticks.
    """

    def __init__(self, watcher: GameWatcher, app_running: Callable[[], bool], spawn: Callable[[], object],
                 clock: Callable[[], float] = time.monotonic, cooldown: float = SPAWN_COOLDOWN_S,
                 max_starts: int = MAX_STARTS_PER_GAME):
        self.watcher = watcher
        self.app_running = app_running
        self.spawn = spawn
        self.clock = clock
        self.cooldown = cooldown
        self.max_starts = max_starts
        self.paused = False
        self.starts = 0
        self._spawned_at: float | None = None

    def pause(self) -> None:
        """The user closed the app. Only meaningful while the game is up: the pause lasts until the
        game session ends, so one sent with the game absent would silence the next session."""
        if self.watcher.present:
            self.paused = True

    def tick(self) -> bool:
        """Poll; True if the app was started."""
        if self.watcher.poll() == "stopped":  # the game session is over: the next one starts fresh
            self.paused = False
            self.starts = 0
        if not (self.watcher.probed and self.watcher.present) or self.paused or self.starts >= self.max_starts:
            return False
        now = self.clock()
        if self._spawned_at is not None and now - self._spawned_at < self.cooldown:
            return False
        if self.app_running():
            return False
        self._spawned_at = now
        self.starts += 1
        self.spawn()
        return True


class EntryGuard:
    """Makes the waiter leave when the login entry no longer names its own program.

    Waiters are per data folder, so the dev checkout's and the exe's can both be running. Only the
    install the Run value names should have one: after the entry is removed or taken by another
    install, the old waiter would otherwise stay and start a second app with the new one. Checked
    every ``every`` ticks; a registry that can't be read, or a value we can't parse, changes nothing.
    """

    def __init__(self, own_command: str, quit_: Callable[[], object],
                 value: Callable[[], str | None] = login_entry_value, every: int = 5,
                 handover: Callable[[str], object] = spawn_command):
        self.own_command = own_command
        self.quit = quit_
        self.value = value
        self.every = every
        self.handover = handover  # starts the waiter the entry names now (it waits for us to exit)
        self.ticks = 0
        self.left = False

    def tick(self) -> bool:
        """True once the waiter has been told to quit."""
        self.ticks += 1
        if self.left or self.ticks % self.every:
            return self.left
        try:
            current = self.value()
        except OSError:
            return False
        if current is not None:  # no entry at all: leave; an entry we can't read as ours or theirs: stay
            ours, theirs = command_target(self.own_command), command_target(current)
            if ours is None or theirs is None or _same_path(theirs, ours):
                return False
        self.left = True
        if current is None:
            log.info("the login entry is gone: quitting")
        else:  # another install (a newer build, say) took it: start its waiter so the game still opens it
            log.info("the login entry now names %s: handing over and quitting", current)
            self.handover(current)
        self.quit()
        return True


class WaiterTray:
    """The waiter's tray icon. ``ensure()`` makes it once the system tray exists (at login it may not yet)."""

    def __init__(self, app, icon, open_now: Callable[[], None]):
        self.app = app
        self.icon = icon
        self.open_now = open_now
        self.tray = None
        self.menu = None

    def ensure(self) -> bool:
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        if self.tray is not None:
            return True
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        tray = QSystemTrayIcon(self.icon)
        tray.setToolTip("mc-tracker: waiting for MapleStory")
        menu = QMenu()
        menu.addAction("Open now", self.open_now)
        menu.addAction("Quit", self.app.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda r: self.open_now() if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        tray.show()
        self.app.aboutToQuit.connect(tray.hide)  # no ghost icon left behind
        self.tray, self.menu = tray, menu
        return True


def waiter_verbs(quit_: Callable[[], object], open_now: Callable[[], object], logic: WaiterLogic) -> dict:
    """The messages the waiter acts on; anything else (empty, unknown) is ignored."""
    return {"quit": quit_, "open": open_now, "pause": logic.pause}


def run_waiter(app, icon, window_title: str, forward: tuple[str, ...] = (), swap: bool = True) -> int:
    """No windows: a tray icon, and a poll that starts the app whenever the game is up without it.

    ``app`` is the QApplication, ``icon`` its QIcon, ``forward`` extra args for the app it starts.
    ``swap``: hand over to a staged build (off after install.ps1 couldn't swap: ``--no-swap``).
    """
    inst = SingleInstance(instance_name("waiter"))
    deadline = time.monotonic() + HANDOVER_WAIT_S
    while not inst.acquire():  # a waiter handing over to us exits within a moment; any other one stays
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.5)
    app.setQuitOnLastWindowClosed(False)

    def open_now() -> None:  # explicit: bring the running app forward, or start it
        if app_running():
            SingleInstance.send(instance_name("app"), "raise")
        else:
            spawn_app(forward)

    logic = WaiterLogic(GameWatcher(game_probe(window_title)), app_running, lambda: spawn_app(forward))
    verbs = waiter_verbs(app.quit, open_now, logic)
    inst.message.connect(lambda text: verbs.get(text, lambda: None)())

    tray = WaiterTray(app, icon, open_now)
    guard = EntryGuard(run_command(forward), app.quit)
    swapper = SwapWatch(Path(sys.executable) if paths.frozen() and swap else None,
                     subprocess.list2cmdline([*forward, FOLLOW_ARG]),
                     logic.watcher.probe, app_running, app.quit)
    timer = QTimer(app)
    timer.setInterval(int(TICK_S * 1000))

    def tick() -> None:
        if tray.tray is None and (tick.n % 5 == 0):  # no system tray yet (login): look again every few seconds
            tray.ensure()
        tick.n += 1
        if guard.tick():
            return
        if tick.n % 5 == 1 and swapper.check():  # (also on the first tick: a waiter started at login swaps first)
            return
        logic.tick()

    tick.n = 0
    timer.timeout.connect(tick)
    timer.start()
    tray.ensure()
    app._waiter = (inst, tray, timer, logic, guard, swapper)  # keep alive
    return app.exec()


class GameFollower(QObject):
    """Inside the running app: polls the game and reports ``started`` / ``stopped`` (see ``GameWatcher``)."""

    started = Signal()
    stopped = Signal()
    present = Signal()  # on every probe while the game is up

    def __init__(self, probe: Callable[[], bool], parent=None, interval: float = POLL_S):
        super().__init__(parent)
        self.watcher = GameWatcher(probe, interval=interval)
        self._timer = QTimer(self)
        self._timer.setInterval(int(min(interval, TICK_S) * 1000))
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
        if self.watcher.probed and self.watcher.present:
            self.present.emit()
