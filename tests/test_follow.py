"""Start and stop with MapleStory: watcher state machine, login entry, single instance, the menu item."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMainWindow

from mctracker import app as app_mod
from mctracker import follow, paths
from mctracker.items import AppSettings


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# --- GameWatcher ------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_watcher(**kw):
    state = {"seen": False, "calls": 0}

    def probe():
        state["calls"] += 1
        return state["seen"]

    clock = Clock()
    return follow.GameWatcher(probe, clock, interval=3.0, **kw), state, clock


def step(w, clock, seconds=3.0):
    clock.t += seconds
    return w.poll()


def test_absent_present_absent():
    w, state, clock = make_watcher()
    assert w.poll() is None and not w.present
    state["seen"] = True
    assert step(w, clock) == "started" and w.present
    assert step(w, clock) is None  # still there
    state["seen"] = False
    for _ in range(follow.MISSES_TO_STOP - 1):
        assert step(w, clock) is None  # short of the grace period
    assert step(w, clock) == "stopped" and not w.present
    assert step(w, clock) is None
    state["seen"] = True
    assert step(w, clock) == "started"  # the next launch opens it again


def test_a_single_missed_probe_does_not_stop():
    w, state, clock = make_watcher()
    state["seen"] = True
    assert w.poll() == "started"
    state["seen"] = False
    for _ in range(follow.MISSES_TO_STOP - 1):
        assert step(w, clock) is None
    state["seen"] = True
    assert step(w, clock) is None  # back: the miss counter resets
    state["seen"] = False
    for _ in range(follow.MISSES_TO_STOP - 1):
        assert step(w, clock) is None
    assert w.present


def test_grace_is_about_fifteen_seconds():
    assert 14 <= follow.MISSES_TO_STOP * follow.POLL_S <= 16


def test_probes_at_most_once_per_interval():
    w, state, clock = make_watcher()
    for _ in range(10):
        w.poll()
    assert state["calls"] == 1
    clock.t += 2.9
    w.poll()
    assert state["calls"] == 1
    clock.t += 0.2
    w.poll()
    assert state["calls"] == 2


def test_failing_probe_keeps_the_state():
    clock = Clock()
    boom = {"on": False}

    def probe():
        if boom["on"]:
            raise RuntimeError("enum failed")
        return True

    w = follow.GameWatcher(probe, clock, interval=3.0)
    assert w.poll() == "started"
    boom["on"] = True
    for _ in range(4):
        assert step(w, clock) is None
    assert w.present


def test_no_stopped_before_the_game_was_seen():
    w, _state, clock = make_watcher()
    assert [step(w, clock) for _ in range(5)] == [None] * 5


# --- login entry (winreg mocked) ----------------------------------------------------


class FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1
    REG_SZ = 1

    def __init__(self, key_exists=True):
        self.keys = {follow.RUN_KEY: {}} if key_exists else {}

    def CreateKeyEx(self, root, path, res, access):
        assert root == "HKCU" and access == self.KEY_SET_VALUE
        return FakeKey(self.keys.setdefault(path, {}))

    def OpenKey(self, root, path, res, access):
        assert root == "HKCU"
        if path not in self.keys:
            raise FileNotFoundError(path)
        return FakeKey(self.keys[path])

    def SetValueEx(self, key, name, res, kind, value):
        assert kind == self.REG_SZ
        key.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], self.REG_SZ


def test_login_entry_add_and_remove():
    reg = FakeReg()
    assert not follow.login_entry_present(reg)
    follow.enable_login_entry(r'"C:\x\mc-tracker.exe" --follow-game', reg)
    assert reg.keys[follow.RUN_KEY] == {"mc-tracker": r'"C:\x\mc-tracker.exe" --follow-game'}
    assert follow.login_entry_present(reg)
    follow.enable_login_entry(r'"C:\y\mc-tracker.exe" --follow-game', reg)  # re-enable replaces, never duplicates
    assert list(reg.keys[follow.RUN_KEY].values()) == [r'"C:\y\mc-tracker.exe" --follow-game']
    follow.disable_login_entry(reg)
    assert reg.keys[follow.RUN_KEY] == {}
    assert not follow.login_entry_present(reg)


def test_disable_when_nothing_was_installed():
    follow.disable_login_entry(FakeReg())
    follow.disable_login_entry(FakeReg(key_exists=False))


def test_only_our_value_is_touched():
    reg = FakeReg()
    reg.keys[follow.RUN_KEY]["OtherApp"] = "other.exe"
    follow.enable_login_entry("x", reg)
    follow.disable_login_entry(reg)
    assert reg.keys[follow.RUN_KEY] == {"OtherApp": "other.exe"}


def test_run_command_frozen_points_at_the_exe(monkeypatch):
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"D:\Maple-dist\mc-tracker\mc-tracker.exe")
    assert follow.run_command() == r"D:\Maple-dist\mc-tracker\mc-tracker.exe --follow-game"
    assert follow.app_command() == [r"D:\Maple-dist\mc-tracker\mc-tracker.exe"]


def test_run_command_keeps_the_hotkey(monkeypatch):
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"C:\x\mc-tracker.exe")
    assert follow.run_command(("--hotkey", "ctrl+b")) == r"C:\x\mc-tracker.exe --hotkey ctrl+b --follow-game"


def test_run_command_from_source_uses_pythonw(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "frozen", lambda: False)
    py = tmp_path / "Program Files" / "python.exe"
    py.parent.mkdir()
    py.touch()
    (py.parent / "pythonw.exe").touch()
    monkeypatch.setattr(sys, "executable", str(py))
    assert follow.run_command() == f'"{py.parent / "pythonw.exe"}" -m mctracker --follow-game'


# --- setting ----------------------------------------------------------------------


def test_follow_game_defaults_off_and_round_trips(tmp_path):
    p = tmp_path / "app.json"
    assert AppSettings(p).follow_game is False
    s = AppSettings(p)
    s.follow_game = True
    s.overlay_scale = 1.4
    s.save()
    again = AppSettings(p)
    assert again.follow_game is True and again.overlay_scale == 1.4


def test_old_settings_file_without_the_key(tmp_path):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"overlay_scale": 1.2}))
    s = AppSettings(p)
    assert s.follow_game is False and s.problem is None


def test_bad_follow_game_value_is_backed_up(tmp_path):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"follow_game": "yes"}))
    s = AppSettings(p)
    assert s.follow_game is False and s.problem and "follow_game" in s.problem


# --- single instance (real local sockets) ------------------------------------------


def pump(cond, timeout_ms=8000):
    """Run a real event loop until ``cond()`` or the timeout (processEvents alone doesn't serve a local server reliably)."""
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(20)
    poll.timeout.connect(lambda: loop.quit() if cond() else None)
    poll.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    poll.stop()
    return cond()


def test_second_instance_hands_over_and_is_refused(qapp):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    first = follow.SingleInstance(name)
    got = []
    first.message.connect(got.append)
    assert first.acquire() is True
    # the second start is a real process, as in use; this one keeps serving the first's socket
    code = "; ".join([
        "from PySide6.QtWidgets import QApplication",
        "from mctracker import follow",
        "app = QApplication([])",
        f"print(follow.SingleInstance({name!r}).acquire('raise'))",
    ])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert pump(lambda: got == ["raise"] and proc.poll() is not None)
        assert proc.stdout.read().strip() == "False"
    finally:
        proc.kill()
        first.release()


def test_name_is_free_again_after_the_owner_goes(qapp):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    first = follow.SingleInstance(name)
    assert first.acquire()
    assert follow.SingleInstance(name).acquire() is False  # the mutex says it is taken, listening or not
    first.release()
    again = follow.SingleInstance(name)
    assert again.acquire() is True
    again.release()


def test_send_is_true_only_when_the_owner_acknowledged(qapp):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    first = follow.SingleInstance(name)
    got = []
    first.message.connect(got.append)
    assert first.acquire()
    code = "; ".join([
        "from PySide6.QtWidgets import QApplication",
        "from mctracker import follow",
        "app = QApplication([])",
        f"print(follow.SingleInstance.send({name!r}, 'ping'))",
    ])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)
    try:
        assert pump(lambda: proc.poll() is not None)
        assert proc.stdout.read().strip() == "True" and got == ["ping"]
    finally:
        proc.kill()
    first.release()
    assert follow.SingleInstance.send(name, "ping") is False  # nobody home


def test_instance_names_are_per_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("MC_TRACKER_HOME", str(tmp_path / "a"))
    a = follow.instance_name("app")
    monkeypatch.setenv("MC_TRACKER_HOME", str(tmp_path / "b"))
    assert follow.instance_name("app") != a
    assert follow.instance_name("waiter") != follow.instance_name("app")


def test_a_busy_app_that_cannot_answer_still_counts_as_running(qapp, monkeypatch):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    monkeypatch.setattr(follow, "instance_name", lambda role: name)
    assert follow.app_running() is False
    app = follow.SingleInstance(name)
    assert app.acquire()  # and its event loop never runs here: it cannot answer anything
    try:
        assert follow.app_running() is True
        assert follow.SingleInstance.send(name, "ping") is False  # a socket check would have said "gone"
    finally:
        app.release()
    assert follow.app_running() is False


def _no_mutex(monkeypatch):
    monkeypatch.setattr(follow, "_create_mutex", lambda name: (0, False))
    monkeypatch.setattr(follow, "mutex_exists", lambda name: False)


def test_without_a_mutex_a_running_app_is_still_detected_and_refuses_a_second(qapp, monkeypatch):
    """The fallback: no named mutex (denied, or not Windows). The lock file stands in for it."""
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    monkeypatch.setattr(follow, "instance_name", lambda role: name)
    _no_mutex(monkeypatch)
    assert follow.app_running() is False
    first = follow.SingleInstance(name)
    assert first.acquire()  # its event loop never runs here: it cannot answer anything
    try:
        assert follow.app_running() is True
        assert follow.SingleInstance(name).acquire() is False  # never two apps
        assert follow.SingleInstance(name).acquire() is False  # (and the refused one left nothing locked)
        assert follow.app_running() is True
    finally:
        first.release()
    assert follow.app_running() is False
    again = follow.SingleInstance(name)
    assert again.acquire() is True
    again.release()


def test_a_lock_held_by_another_process_is_seen_and_refuses_a_second_app(qapp, monkeypatch):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    code = "; ".join([
        "import sys",
        "from mctracker import follow",
        f"fd, held = follow._take_lock({name!r})",
        "print(fd is not None and not held, flush=True)",
        "sys.stdin.readline()",
    ])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    proc = subprocess.Popen([sys.executable, "-c", code], env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "True"
        assert follow.lock_held(name) is True
        monkeypatch.setattr(follow, "_create_mutex", lambda n: (0, False))
        assert follow.SingleInstance(name).acquire() is False
    finally:
        proc.kill()
        proc.wait()
    assert pump(lambda: not follow.lock_held(name), 5000)  # the OS drops it with the process (a moment later)
    again = follow.SingleInstance(name)
    assert again.acquire() is True
    again.release()


def test_a_lock_that_is_busy_for_a_moment_is_retried(monkeypatch):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    real = follow._lock_fd
    calls = []

    def flaky(fd, lock):
        calls.append(lock)
        if len(calls) == 1:
            raise OSError("busy")
        return real(fd, lock)

    monkeypatch.setattr(follow, "_lock_fd", flaky)
    fd, held = follow._take_lock(name)
    try:
        assert fd is not None and held is False and calls[:2] == [True, True]
    finally:
        follow._drop_lock(name, fd)
    monkeypatch.setattr(follow, "_lock_fd", lambda fd, lock: (_ for _ in ()).throw(OSError("busy")))
    assert follow._take_lock(name) == (None, True)  # still busy after the retries: someone owns it
    follow._lock_path(name).unlink(missing_ok=True)


def test_a_waiter_without_a_mutex_does_not_start_a_second_app(qapp, monkeypatch):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    monkeypatch.setattr(follow, "instance_name", lambda role: name)
    _no_mutex(monkeypatch)
    app = follow.SingleInstance(name)
    assert app.acquire()
    try:
        spawned = []
        watcher = follow.GameWatcher(lambda: True, interval=0)
        logic = follow.WaiterLogic(watcher, follow.app_running, lambda: spawned.append(1))
        assert logic.tick() is False and spawned == []
    finally:
        app.release()


def test_with_nothing_to_claim_a_name_an_unlistenable_name_is_refused(qapp, monkeypatch):
    """No mutex and no lock file: the socket is the last guard. Where a taken name can't be listened
    on (it can on Windows, so this is stubbed), a silent owner must not be replaced."""
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    _no_mutex(monkeypatch)

    class Taken:
        removed = []

        def __init__(self, parent=None):
            pass

        def listen(self, n):
            return False

        def errorString(self):  # noqa: N802 - Qt API
            return "in use"

        @staticmethod
        def removeServer(n):  # noqa: N802 - Qt API
            Taken.removed.append(n)

    monkeypatch.setattr(follow, "QLocalServer", Taken)
    monkeypatch.setattr(follow, "_take_lock", lambda n: (None, False))
    assert follow.SingleInstance(name).acquire() is False
    assert Taken.removed == []  # a live owner's socket is not ours to remove
    monkeypatch.undo()
    monkeypatch.setattr(follow, "QLocalServer", Taken)
    _no_mutex(monkeypatch)
    inst = follow.SingleInstance(name)
    assert inst.acquire() is True  # the lock file is ours: it is just the socket that failed
    inst.release()


def test_a_busy_app_is_not_started_again_by_the_waiter(qapp, monkeypatch):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    monkeypatch.setattr(follow, "instance_name", lambda role: name)
    app = follow.SingleInstance(name)
    assert app.acquire()
    try:
        state = {"spawned": 0}
        clock = Clock()
        logic = follow.WaiterLogic(follow.GameWatcher(lambda: True, clock, interval=3.0), follow.app_running,
                                   lambda: state.__setitem__("spawned", state["spawned"] + 1), clock)
        for _ in range(20):
            clock.t += 3
            logic.tick()
        assert state["spawned"] == 0
    finally:
        app.release()


# --- the waiter ---------------------------------------------------------------------


def make_waiter(app_up):
    state = {"seen": True, "spawned": 0}
    clock = Clock()
    watcher = follow.GameWatcher(lambda: state["seen"], clock, interval=3.0)

    def spawn():
        state["spawned"] += 1

    logic = follow.WaiterLogic(watcher, lambda: app_up["v"], spawn, clock, cooldown=20.0)
    return logic, state, clock


def test_waiter_starts_the_app_when_the_game_is_up_without_it():
    app_up = {"v": False}
    logic, state, clock = make_waiter(app_up)
    assert logic.tick() is True and state["spawned"] == 1  # game appears, no app
    app_up["v"] = True
    for _ in range(10):
        clock.t += 3
        assert logic.tick() is False  # the app is there: nothing to do
    assert state["spawned"] == 1


def test_waiter_restores_an_app_that_quit_while_the_game_stayed_up():
    """Quick game restart: the app's poll saw the gap and quit; the waiter never saw 'stopped'."""
    app_up = {"v": True}
    logic, state, clock = make_waiter(app_up)
    logic.watcher.poll()
    assert state["spawned"] == 0
    state["seen"] = False
    clock.t += 3
    logic.tick()  # one miss at the waiter
    state["seen"] = True
    app_up["v"] = False  # the app quit on its side
    clock.t += 3
    assert logic.tick() is True and state["spawned"] == 1


def test_waiter_waits_for_a_starting_app_before_looking_again():
    app_up = {"v": False}
    logic, state, clock = make_waiter(app_up)
    logic.tick()
    for _ in range(5):  # 15 s: the app is still coming up
        clock.t += 3
        assert logic.tick() is False
    assert state["spawned"] == 1
    clock.t += 6  # past the cooldown and still no app: it died, start another
    assert logic.tick() is True and state["spawned"] == 2


def test_pause_keeps_the_waiter_quiet_until_the_game_has_gone():
    app_up = {"v": True}
    logic, state, clock = make_waiter(app_up)
    logic.tick()
    app_up["v"] = False  # the user closed the app; the app tells the waiter
    logic.pause()
    for _ in range(30):  # game still up for a minute and a half
        clock.t += 3
        assert logic.tick() is False
    assert state["spawned"] == 0
    state["seen"] = False  # the game closes ...
    for _ in range(follow.MISSES_TO_STOP + 1):
        clock.t += 3
        logic.tick()
    assert not logic.paused
    state["seen"] = True  # ... and the next launch opens the app again
    clock.t += 3
    assert logic.tick() is True and state["spawned"] == 1


def test_a_pause_with_the_game_absent_is_ignored():
    """Setting on, the user opens the app before the game and closes it: that must not silence the next game."""
    app_up = {"v": False}
    logic, state, clock = make_waiter(app_up)
    state["seen"] = False
    for _ in range(3):  # no game
        clock.t += 3
        logic.tick()
    logic.pause()
    assert not logic.paused
    state["seen"] = True  # MapleStory starts: the app must open
    clock.t += 3
    assert logic.tick() is True and state["spawned"] == 1


def test_pause_does_not_survive_a_short_gap_in_the_game():
    """The waiter only forgets a pause once the game is really gone (the full grace period)."""
    app_up = {"v": True}
    logic, state, clock = make_waiter(app_up)
    logic.tick()
    logic.pause()
    app_up["v"] = False
    state["seen"] = False
    for _ in range(follow.MISSES_TO_STOP - 1):
        clock.t += 3
        logic.tick()
    state["seen"] = True
    clock.t += 3
    logic.tick()
    assert logic.paused and state["spawned"] == 0


def test_a_crashing_app_is_restarted_only_a_few_times_per_game_session():
    app_up = {"v": False}  # every start dies at once
    logic, state, clock = make_waiter(app_up)
    for _ in range(60):  # three minutes
        clock.t += 3
        logic.tick()
    assert state["spawned"] == follow.MAX_STARTS_PER_GAME == 3
    state["seen"] = False  # the game ends: a new session gets its starts back
    for _ in range(follow.MISSES_TO_STOP + 1):
        clock.t += 3
        logic.tick()
    state["seen"] = True
    clock.t += 3
    assert logic.tick() is True and state["spawned"] == 4


def test_the_waiter_acts_on_its_verbs_only():
    logic, state, clock = make_waiter({"v": False})
    logic.tick()  # the game is up, so a pause counts
    called = []
    verbs = follow.waiter_verbs(lambda: called.append("quit"), lambda: called.append("open"), logic)
    assert set(verbs) == {"quit", "open", "pause"}
    for text in ("", "ping", "raise", "bogus"):  # an empty or unknown message must do nothing
        verbs.get(text, lambda: None)()
    assert called == [] and not logic.paused
    verbs["open"]()
    verbs["quit"]()
    assert called == ["open", "quit"]
    verbs["pause"]()
    assert logic.paused


def test_tray_is_set_up_late_when_the_system_tray_appears(qapp, monkeypatch):
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon

    avail = {"v": False}
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: avail["v"]))
    tray = follow.WaiterTray(qapp, QIcon(), lambda: None)
    assert tray.ensure() is False and tray.tray is None
    avail["v"] = True
    assert tray.ensure() is True and tray.tray is not None
    first = tray.tray
    assert tray.ensure() is True and tray.tray is first  # made once
    tray.tray.hide()


def test_waiter_does_nothing_without_the_game():
    app_up = {"v": False}
    logic, state, clock = make_waiter(app_up)
    state["seen"] = False
    for _ in range(5):
        clock.t += 3
        assert logic.tick() is False
    assert state["spawned"] == 0


# --- follower + menu item ----------------------------------------------------------


def test_follower_emits_started_and_stopped(qapp):
    seen = {"v": False}
    f = follow.GameFollower(lambda: seen["v"], interval=0)
    events = []
    f.started.connect(lambda: events.append("started"))
    f.stopped.connect(lambda: events.append("stopped"))
    seen["v"] = True
    f._tick()
    seen["v"] = False
    for _ in range(follow.MISSES_TO_STOP):
        f._tick()
    assert events == ["started", "stopped"]


class FakeTracker:
    def __init__(self, settings):
        self.settings = settings


class _Sig:
    def connect(self, fn):
        self.fn = fn


class FakeInstance:
    def __init__(self):
        self.message = _Sig()


class Win(QMainWindow):
    def __init__(self):
        super().__init__()
        self.notices = []
        self.raised = 0
        self.on_close = None

    def closeEvent(self, e):  # noqa: N802 - Qt API
        if self.on_close:
            self.on_close()
        super().closeEvent(e)

    def show_notice(self, msg, timeout_s=0):
        self.notices.append(msg)

    def raise_(self):
        self.raised += 1


class FakeApp(QObject):
    """What ``_wire_follow_game`` needs from the QApplication, without touching the shared one."""

    aboutToQuit = Signal()

    def __init__(self):
        super().__init__()
        self.quits = 0

    def quit(self):
        self.quits += 1


_made: list = []


@pytest.fixture(autouse=True)
def _quiet_followers(monkeypatch, qapp):
    """No test sees the real game or the real registry's Run key; every window and follower made by
    ``wire`` is stopped and deleted afterwards."""
    monkeypatch.setattr(follow, "game_probe", lambda title: (lambda: False))

    def no_registry():
        raise AssertionError("a test reached the real registry")

    monkeypatch.setattr(follow, "_winreg", no_registry)
    yield
    while _made:
        win, overlay = _made.pop()
        for f in win.findChildren(follow.GameFollower):
            f.stop()
        win.close()
        win.deleteLater()
        overlay.deleteLater()
    QApplication.processEvents()


def followers(win):
    return win.findChildren(follow.GameFollower)


def wire(qapp, tmp_path, monkeypatch, *, on=False, entry=None, value="match", instance="fake", extra=(), feed=None):
    """``entry``: whether the login entry exists (default: as the setting says). ``value``: what it
    runs ("match": the current command)."""
    calls = []
    entry = on if entry is None else entry
    if value == "match":
        value = follow.run_command(extra)
    monkeypatch.setattr(follow, "enable_login_entry", lambda *a, **k: calls.append(("enable", a)))
    monkeypatch.setattr(follow, "disable_login_entry", lambda *a, **k: calls.append("disable"))
    monkeypatch.setattr(follow, "login_entry_value", lambda *a, **k: value if entry else None)
    monkeypatch.setattr(follow, "spawn_waiter", lambda *a: calls.append("spawn_waiter"))
    monkeypatch.setattr(follow, "ask_waiter_to_quit", lambda: calls.append("quit_waiter"))
    monkeypatch.setattr(follow, "ask_waiter_to_pause", lambda: calls.append("pause_waiter"))
    settings = AppSettings(tmp_path / "app.json")
    settings.follow_game = on
    win = Win()
    overlay = QMainWindow()
    _made.append((win, overlay))
    inst = FakeInstance() if instance == "fake" else instance
    fake_app = win.fapp = FakeApp()
    if feed is not None:
        fake_app.feed = feed
    app_mod._wire_follow_game(fake_app, win, overlay, FakeTracker(settings), "MapleStory", inst, extra)
    actions = [a for m in win.menuBar().actions() if m.menu() for a in m.menu().actions()]
    action = next(a for a in actions if a.text() == "Start and stop with MapleStory")
    return win, action, settings, calls, inst


def test_menu_item_is_off_by_default_and_does_nothing_until_used(qapp, tmp_path, monkeypatch):
    _win, action, settings, calls, _ = wire(qapp, tmp_path, monkeypatch)
    assert action.isCheckable() and not action.isChecked()
    assert calls == [] and settings.follow_game is False


def test_turning_on_installs_and_persists_then_off_removes(qapp, tmp_path, monkeypatch):
    _win, action, _settings, calls, _ = wire(qapp, tmp_path, monkeypatch)
    action.setChecked(True)
    assert [c if isinstance(c, str) else c[0] for c in calls] == ["enable", "spawn_waiter"]
    assert AppSettings(tmp_path / "app.json").follow_game is True
    action.setChecked(False)
    assert calls[2:] == ["disable", "quit_waiter"]
    assert AppSettings(tmp_path / "app.json").follow_game is False


def test_a_custom_hotkey_goes_into_the_login_entry(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", r"C:\x\mc-tracker.exe")
    _win, action, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, extra=("--hotkey", "ctrl+b"))
    action.setChecked(True)
    assert calls[0] == ("enable", (r"C:\x\mc-tracker.exe --hotkey ctrl+b --follow-game",))


def test_registry_failure_leaves_it_off(qapp, tmp_path, monkeypatch):
    win, action, settings, _calls, _ = wire(qapp, tmp_path, monkeypatch)

    def refuse(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(follow, "enable_login_entry", refuse)
    action.setChecked(True)
    assert not action.isChecked() and settings.follow_game is False
    assert "denied" in win.notices[-1]


def test_failing_to_remove_the_entry_keeps_it_on(qapp, tmp_path, monkeypatch):
    win, action, settings, _calls, _ = wire(qapp, tmp_path, monkeypatch, on=True)

    def refuse(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(follow, "disable_login_entry", refuse)
    action.setChecked(False)
    assert action.isChecked() and settings.follow_game is True
    assert "locked" in win.notices[-1]


def test_the_registry_wins_over_a_reset_settings_file(qapp, tmp_path, monkeypatch):
    """A damaged app.json resets the flag but the Run entry is still there: the menu shows it, and the file follows."""
    _win, action, settings, calls, _ = wire(qapp, tmp_path, monkeypatch, on=False, entry=True)
    assert action.isChecked() and settings.follow_game is True
    assert AppSettings(tmp_path / "app.json").follow_game is True
    assert calls == []  # reconciling writes nothing to the registry


def test_a_removed_entry_switches_the_setting_off(qapp, tmp_path, monkeypatch):
    _win, action, settings, _calls, _ = wire(qapp, tmp_path, monkeypatch, on=True, entry=False)
    assert not action.isChecked() and settings.follow_game is False


def test_no_instance_guard_is_fine(qapp, tmp_path, monkeypatch):
    """--crash-test starts without the single-instance guard."""
    _win, action, _s, _c, _ = wire(qapp, tmp_path, monkeypatch, instance=None)
    assert action.isCheckable()


def test_only_raise_brings_the_window_forward(qapp, tmp_path, monkeypatch):
    win, _a, _s, _c, inst = wire(qapp, tmp_path, monkeypatch)
    inst.message.fn("ping")
    assert win.raised == 0
    inst.message.fn("raise")
    assert win.raised == 1


def test_closing_the_app_tells_the_waiter_to_pause(qapp, tmp_path, monkeypatch):
    win, _action, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True)
    win.fapp.aboutToQuit.emit()
    assert calls.count("pause_waiter") == 1


def test_no_pause_when_the_setting_is_off(qapp, tmp_path, monkeypatch):
    win, _action, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=False)
    win.fapp.aboutToQuit.emit()
    assert "pause_waiter" not in calls


def test_the_game_closing_does_not_pause_the_waiter(qapp, tmp_path, monkeypatch):
    win, _action, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True)
    followers(win)[0].stopped.emit()  # what the follower does when the game has been gone for the grace period
    win.fapp.aboutToQuit.emit()  # (the real app emits this after quit)
    assert win.fapp.quits == 1 and "pause_waiter" not in calls


def test_a_stale_login_entry_is_rewritten(qapp, tmp_path, monkeypatch):
    _win, action, settings, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True, value=r'"D:\old\mc-tracker.exe" --follow-game')
    assert calls == [("enable", (follow.run_command(),))]
    assert action.isChecked() and settings.follow_game is True  # rewritten, not toggled: no new waiter either


def _install(tmp_path, name="other"):
    exe = tmp_path / name / "mc-tracker.exe"
    exe.parent.mkdir()
    exe.touch()
    return exe, subprocess.list2cmdline([str(exe), "--follow-game"])


def test_another_installs_entry_is_not_taken_over(qapp, tmp_path, monkeypatch):
    exe, theirs = _install(tmp_path)
    win, action, settings, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True, value=theirs)
    assert calls == []  # not rewritten
    assert action.isChecked() and settings.follow_game is True  # the entry is there, so the feature is on
    assert str(exe) in win.notices[-1]


def test_a_moved_or_deleted_exe_entry_is_still_repaired(qapp, tmp_path, monkeypatch):
    exe, theirs = _install(tmp_path)
    exe.unlink()
    win, action, settings, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True, value=theirs)
    assert calls == [("enable", (follow.run_command(),))]
    assert action.isChecked() and win.notices == []


def test_our_own_entry_with_other_options_is_rewritten(qapp, tmp_path, monkeypatch):
    exe, theirs = _install(tmp_path)
    monkeypatch.setattr(paths, "frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(exe))
    win, _a, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True, value=theirs, extra=("--hotkey", "ctrl+b"))
    assert calls == [("enable", (follow.run_command(("--hotkey", "ctrl+b")),))]
    assert win.notices == []


def test_check_login_entry_decisions(tmp_path):
    exe, theirs = _install(tmp_path)
    gone = tmp_path / "gone" / "mc-tracker.exe"  # the drive is there, the exe is not
    ours = subprocess.list2cmdline([str(gone), "--follow-game"])
    assert follow.check_login_entry(ours, ours) == "keep"
    assert follow.check_login_entry(theirs, ours) == "foreign"
    assert follow.check_login_entry(ours, theirs) == "rewrite"  # the registered program is gone
    assert follow.check_login_entry("", ours) == "rewrite"
    assert follow.command_target(theirs) == exe
    assert follow.command_target(r"C:\x\a.exe --follow-game") == Path(r"C:\x\a.exe")  # gone
    assert follow.command_target("pythonw -m mctracker --follow-game") is None  # no program to name


def test_an_unquoted_entry_with_spaces_resolves_by_its_longest_existing_prefix(tmp_path):
    exe = tmp_path / "Program Files" / "mc tracker" / "mc-tracker.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    unquoted = f"{exe} --follow-game"
    assert follow.command_target(unquoted) == exe
    wanted = subprocess.list2cmdline([str(tmp_path / "gone" / "x.exe"), "--follow-game"])
    assert follow.check_login_entry(unquoted, wanted) == "foreign"  # not misread as a missing "...\Program"


def test_an_entry_that_cannot_be_read_is_kept(tmp_path):
    wanted = subprocess.list2cmdline([str(tmp_path / "x.exe"), "--follow-game"])
    assert follow.check_login_entry(r"C:\no such\a --follow-game", wanted) == "foreign"
    assert follow.check_login_entry("pythonw -m mctracker --follow-game", wanted) == "foreign"  # relative
    assert follow.check_login_entry(subprocess.list2cmdline(["relative.exe"]), wanted) == "foreign"


def test_an_exe_on_an_unreachable_drive_is_not_taken_over(tmp_path):
    drive = next((d for d in "ZYXWVUTSRQ" if not Path(f"{d}:\\").exists()), None)
    if drive is None:
        pytest.skip("every drive letter is in use")
    wanted = subprocess.list2cmdline([str(tmp_path / "x.exe"), "--follow-game"])
    offline = subprocess.list2cmdline([f"{drive}:\\mc-tracker\\mc-tracker.exe", "--follow-game"])
    assert follow.check_login_entry(offline, wanted) == "foreign"


def test_the_notice_shows_an_unreadable_entry_as_is(qapp, tmp_path, monkeypatch):
    win, action, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True, value="pythonw -m mctracker --follow-game")
    assert calls == [] and action.isChecked()
    assert "pythonw -m mctracker" in win.notices[-1]


# --- the waiter leaves when the entry is no longer its own -------------------------


def _guard(tmp_path, entry, every=3):
    exe, own = _install(tmp_path, f"mine{len(list(tmp_path.iterdir()))}")
    quits = []
    state = {"value": entry(own) if callable(entry) else entry}
    guard = follow.EntryGuard(own, lambda: quits.append(1), value=lambda: state["value"], every=every)
    return guard, quits, state, own


def test_a_waiter_stays_while_the_entry_names_its_program(tmp_path):
    guard, quits, _state, _own = _guard(tmp_path, lambda own: own)
    assert [guard.tick() for _ in range(9)] == [False] * 9 and quits == []


def test_a_waiter_leaves_when_the_entry_is_removed_or_taken(tmp_path):
    guard, quits, state, own = _guard(tmp_path, lambda own: own)
    state["value"] = None  # the other install turned the setting off
    assert [guard.tick() for _ in range(3)] == [False, False, True] and quits == [1]
    assert guard.tick() is True and quits == [1]  # once
    _exe, theirs = _install(tmp_path, "theirs")
    guard, quits, state, _ = _guard(tmp_path, theirs)  # the other install owns it
    assert [guard.tick() for _ in range(3)] == [False, False, True] and quits == [1]


def test_a_waiter_ignores_what_it_cannot_tell(tmp_path):
    guard, quits, state, own = _guard(tmp_path, "pythonw -m mctracker --follow-game")
    assert [guard.tick() for _ in range(6)] == [False] * 6
    state["value"] = own.replace("--follow-game", "--hotkey ctrl+b --follow-game")  # same program, other options
    assert [guard.tick() for _ in range(6)] == [False] * 6

    def boom():
        raise PermissionError("registry")

    guard.value = boom
    assert [guard.tick() for _ in range(6)] == [False] * 6 and quits == []


def test_a_current_login_entry_is_left_alone(qapp, tmp_path, monkeypatch):
    _win, _action, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True)
    assert calls == []


def test_the_pause_goes_out_before_the_window_saves(qapp, tmp_path, monkeypatch):
    """Closing saves the session and stops the capture, which can take seconds in which the app
    answers nothing: the waiter must already know."""
    win, _a, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True)
    win.on_close = lambda: calls.append("closeEvent")
    win.close()
    assert calls == ["pause_waiter", "closeEvent"]
    win.fapp.aboutToQuit.emit()
    assert calls.count("pause_waiter") == 1  # once


def test_the_game_ending_closes_without_a_pause(qapp, tmp_path, monkeypatch):
    win, _a, _s, calls, _ = wire(qapp, tmp_path, monkeypatch, on=True)
    followers(win)[0].stopped.emit()
    assert "pause_waiter" not in calls
