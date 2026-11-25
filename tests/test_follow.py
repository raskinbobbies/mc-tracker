"""Start and stop with MapleStory: watcher state machine, login entry, single instance, the menu item."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer
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


def test_name_is_free_again_after_the_owner_goes(qapp):
    name = f"mc-tracker-test-{uuid.uuid4().hex}"
    first = follow.SingleInstance(name)
    assert first.acquire()
    first._server.close()
    assert follow.SingleInstance(name).acquire() is True


def test_instance_names_are_per_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("MC_TRACKER_HOME", str(tmp_path / "a"))
    a = follow.instance_name("app")
    monkeypatch.setenv("MC_TRACKER_HOME", str(tmp_path / "b"))
    assert follow.instance_name("app") != a
    assert follow.instance_name("waiter") != follow.instance_name("app")


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

    def show_notice(self, msg, timeout_s=0):
        self.notices.append(msg)

    def raise_(self):
        self.raised += 1


def wire(qapp, tmp_path, monkeypatch, *, on=False, entry=None, instance="fake", extra=()):
    """``entry``: whether the login entry exists (default: as the setting says)."""
    calls = []
    entry = on if entry is None else entry
    monkeypatch.setattr(follow, "enable_login_entry", lambda *a, **k: calls.append(("enable", a)))
    monkeypatch.setattr(follow, "disable_login_entry", lambda *a, **k: calls.append("disable"))
    monkeypatch.setattr(follow, "login_entry_present", lambda *a, **k: entry)
    monkeypatch.setattr(follow, "spawn_waiter", lambda *a: calls.append("spawn_waiter"))
    monkeypatch.setattr(follow, "ask_waiter_to_quit", lambda: calls.append("quit_waiter"))
    settings = AppSettings(tmp_path / "app.json")
    settings.follow_game = on
    win = Win()
    overlay = QMainWindow()
    inst = FakeInstance() if instance == "fake" else instance
    app_mod._wire_follow_game(qapp, win, overlay, FakeTracker(settings), "MapleStory", inst, extra)
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


class FakeCap:
    def __init__(self, running, hwnd=None):
        self.running = running
        self.hwnd = hwnd
        self.calls = []

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")


def test_capture_is_retried_when_it_is_not_running(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(follow, "game_probe", lambda title: (lambda: True))
    qapp.feed = FakeCap(running=False, hwnd=123)  # started once, then the window closed
    try:
        wire(qapp, tmp_path, monkeypatch, on=True)
        follower = [c for c in qapp.children() if isinstance(c, follow.GameFollower)][-1]
        follower.watcher.interval = 0
        follower._tick()
        assert qapp.feed.calls == ["stop", "start"]
        qapp.feed.running = True
        follower._tick()
        assert qapp.feed.calls == ["stop", "start"]  # running: left alone
        follower.stop()
    finally:
        del qapp.feed
