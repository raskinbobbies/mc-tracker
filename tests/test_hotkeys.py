"""Overlay click-through lock and the global hotkeys: settings, registration (Win32 faked), the GUI."""

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker import hotkey  # noqa: E402
from mctracker.gui import MainWindow  # noqa: E402
from mctracker.hotkey import (  # noqa: E402
    MOD_NOREPEAT,
    WM_HOTKEY,
    WM_QUIT,
    GlobalHotkey,
    HotkeySet,
    hotkey_problem,
    parse_hotkey,
)
from mctracker.items import HOTKEY_DEFAULTS, AppSettings  # noqa: E402
from mctracker.overlay import Overlay  # noqa: E402
from mctracker.session import Tracker  # noqa: E402
from test_overlay_layout import FlatAssets  # noqa: E402

# --- settings ----------------------------------------------------------------


def test_settings_default_to_unlocked_with_default_hotkeys(tmp_path):
    s = AppSettings(tmp_path / "app.json")
    assert s.overlay_locked is False and s.hotkeys == HOTKEY_DEFAULTS


def test_lock_and_hotkeys_round_trip(tmp_path):
    s = AppSettings(tmp_path / "app.json")
    s.overlay_locked = True
    s.hotkeys = {**HOTKEY_DEFAULTS, "pause": "", "lock": "ctrl+alt+l"}
    s.save()
    again = AppSettings(tmp_path / "app.json")
    assert again.overlay_locked is True and again.problem is None
    assert again.hotkeys == {**HOTKEY_DEFAULTS, "pause": "", "lock": "ctrl+alt+l"}


def test_old_file_gets_new_hotkeys_and_drops_unknown_ones(tmp_path):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"overlay_scale": 1.0, "hotkeys": {"lock": "Ctrl+Alt+K", "nope": "f1"}}))
    s = AppSettings(p)
    assert s.problem is None and s.overlay_locked is False
    assert s.hotkeys == {**HOTKEY_DEFAULTS, "lock": "ctrl+alt+k"}


@pytest.mark.parametrize("raw", [{"overlay_locked": "yes"}, {"hotkeys": ["f9"]}, {"hotkeys": {"lock": 9}}])
def test_bad_values_fall_back_to_defaults_with_a_notice(tmp_path, raw):
    p = tmp_path / "app.json"
    p.write_text(json.dumps(raw))
    s = AppSettings(p)
    assert s.overlay_locked is False and s.hotkeys == HOTKEY_DEFAULTS
    assert s.problem


@pytest.mark.parametrize(("saved", "loaded"), [
    ({"lock": "alt+a"}, {}),  # the price check's: default
    ({"lock": "ctrl+alt+k", "overlay": "Alt+Ctrl+K"}, {"lock": "ctrl+alt+k"}),  # a repeat: default
    ({"pause": "f5"}, {}),  # no modifier: default
    ({"lock": "ctrl+shift+f10"}, {"lock": "ctrl+shift+f10", "overlay": ""}),  # the default is taken too: off
])
def test_loaded_hotkeys_get_the_dialogs_checks(tmp_path, saved, loaded):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"hotkeys": saved}))
    s = AppSettings(p)
    assert s.hotkeys == {**HOTKEY_DEFAULTS, **loaded}
    assert s.problem and "hotkeys." in s.problem
    assert hotkey_problem(s.hotkeys) == ""


def test_defaults_are_valid_distinct_and_leave_price_check_alone():
    assert hotkey_problem(HOTKEY_DEFAULTS, "alt+a") == ""
    assert "price-check" in hotkey_problem({"lock": "Alt+A"}, "alt+a")
    assert "twice" in hotkey_problem({"lock": "ctrl+shift+f9", "overlay": "shift+ctrl+F9"})
    assert "modifier" in hotkey_problem({"lock": "f9"})
    assert hotkey_problem({"lock": "ctrl+nope"})
    assert hotkey_problem({"lock": "", "overlay": "", "pause": ""}) == ""


# --- registration (Win32 faked) ---------------------------------------------


class FakeUser32:
    """RegisterHotKey / GetMessageW / PostThreadMessageW with one queue per thread, like Windows."""

    def __init__(self, taken=()):
        self.taken = {parse_hotkey(s) for s in taken}  # held by another program
        self.registered: dict[tuple[int, int], int] = {}  # combination -> thread id
        self.queues: dict[int, queue.Queue] = {}
        self.lock = threading.Lock()

    def RegisterHotKey(self, hwnd, id_, mods, vk):  # noqa: N802 - Win32 name
        assert hwnd is None and mods & MOD_NOREPEAT
        combo = (mods & ~MOD_NOREPEAT, vk)
        with self.lock:
            if combo in self.taken or combo in self.registered:
                return 0
            tid = threading.get_native_id()
            self.registered[combo] = tid
            self.queues[tid] = queue.Queue()
        return 1

    def UnregisterHotKey(self, hwnd, id_):  # noqa: N802
        tid = threading.get_native_id()
        with self.lock:
            for combo, t in list(self.registered.items()):
                if t == tid:
                    del self.registered[combo]
        return 1

    def GetMessageW(self, pmsg, hwnd, lo, hi):  # noqa: N802
        m = self.queues[threading.get_native_id()].get(timeout=5)
        pmsg._obj.message = m
        return 0 if m == WM_QUIT else 1

    def PostThreadMessageW(self, tid, msg, wp, lp):  # noqa: N802
        self.queues[tid].put(msg)
        return 1

    def press(self, spec):
        self.queues[self.registered[parse_hotkey(spec)]].put(WM_HOTKEY)


def wait_for(cond, timeout=3.0):
    app = QApplication.instance()
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def fake(monkeypatch):
    f = FakeUser32(taken=["ctrl+shift+f10"])
    monkeypatch.setattr(hotkey, "user32", f)
    return f


def test_global_hotkey_reports_a_taken_combination(fake):
    hk = GlobalHotkey("ctrl+shift+f10")
    failed = []
    hk.failed.connect(failed.append)
    hk._run()  # on this thread: registration fails before the message loop
    assert failed == ["Hotkey ctrl+shift+f10 is already in use"]
    hk.stop()  # nothing to stop; returns at once


def test_global_hotkey_reports_an_unknown_key(fake):
    hk = GlobalHotkey("ctrl+shift+nope")
    failed = []
    hk.failed.connect(failed.append)
    hk._run()
    assert failed and "nope" in failed[0] and not fake.registered


def test_hotkey_set_runs_actions_on_the_gui_thread_and_survives_a_failure(fake):
    hits, fails = [], []
    gui = threading.get_ident()
    hs = HotkeySet({name: (lambda name=name: hits.append((name, threading.get_ident() == gui)))
                    for name in ("lock", "overlay", "pause")})
    hs.failed.connect(fails.append)
    hs.apply({"lock": "ctrl+shift+f9", "overlay": "ctrl+shift+f10", "pause": ""})
    wait_for(lambda: fails and parse_hotkey("ctrl+shift+f9") in fake.registered)
    assert fails == ["Hotkey ctrl+shift+f10 is already in use"]  # one notice; the lock key still works
    fake.press("ctrl+shift+f9")
    wait_for(lambda: hits)
    assert hits == [("lock", True)]
    # New combinations: the old one is released first, so even the same key registers again.
    hs.apply({"lock": "ctrl+shift+f9", "pause": "ctrl+shift+f11"})
    wait_for(lambda: len(fake.registered) == 2)
    fake.press("ctrl+shift+f11")
    wait_for(lambda: len(hits) == 2)
    assert hits[1] == ("pause", True) and len(fails) == 1
    hs.stop()
    wait_for(lambda: not fake.registered)


# --- overlay lock and the menu -------------------------------------------------


def make(tmp_path):
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    assets = FlatAssets(tmp_path)
    ov = Overlay(tr, assets)
    return tr, ov, MainWindow(tr, ov, assets, "alt+a")


def transparent(ov):
    return bool(ov.windowFlags() & Qt.WindowType.WindowTransparentForInput)


def test_lock_makes_the_overlay_click_through_and_persists(tmp_path):
    tr, ov, w = make(tmp_path)
    ov.show()
    assert not transparent(ov) and not w.lock_action.isChecked()
    ov.toggle_locked()  # the hotkey
    assert transparent(ov) and ov.isVisible() and w.lock_action.isChecked()
    assert AppSettings(tmp_path / "app.json").overlay_locked is True
    assert "locked" in w.notice.text()
    w.lock_action.setChecked(False)  # the menu
    assert not transparent(ov) and ov.isVisible() and not ov.locked
    assert AppSettings(tmp_path / "app.json").overlay_locked is False
    # The next start opens locked.
    tr.settings.overlay_locked = True
    ov2 = Overlay(tr, FlatAssets(tmp_path))
    assert ov2.locked and transparent(ov2)


EXSTYLE_PROBE = textwrap.dedent("""
    import ctypes, json, sys, tempfile, time
    from pathlib import Path
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from mctracker.items import AppSettings
    from mctracker.overlay import Overlay
    from mctracker.session import Tracker
    from test_overlay_layout import FlatAssets
    d = Path(tempfile.mkdtemp())
    ov = Overlay(Tracker(save_dir=d / "s", settings=AppSettings(d / "app.json")), FlatAssets(d))
    def pump():
        end = time.monotonic() + 0.2
        while time.monotonic() < end:
            app.processEvents()
    def state():
        pump()
        ex = ctypes.windll.user32.GetWindowLongW(int(ov.winId()), -20)  # GWL_EXSTYLE
        return {"transparent": bool(ex & 0x20), "visible": ov.isVisible(),
                "no_activate": ov.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)}
    ov.move(200, 150)
    ov.show()
    out = {"shown": state()}
    ov.set_locked(True)
    out["locked"] = state()
    ov.toggle_visible(); pump(); ov.toggle_visible()
    out["reshown"] = state()
    ov.set_locked(False)
    out["unlocked"] = state()
    out["pos"] = ov.pos().toTuple()
    ov.close()
    print(json.dumps(out))
""")


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 window styles")
def test_lock_sets_ws_ex_transparent_on_the_real_window(tmp_path):
    """Offscreen windows have no HWND: check the real extended style in a child process on the Windows platform."""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "QT_QPA_PLATFORM": "windows",
           "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")])}
    run = subprocess.run([sys.executable, "-c", EXSTYLE_PROBE], env=env, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["shown"] == {"transparent": False, "visible": True, "no_activate": True}
    assert out["locked"] == {"transparent": True, "visible": True, "no_activate": True}
    assert out["reshown"] == {"transparent": True, "visible": True, "no_activate": True}
    assert out["unlocked"] == {"transparent": False, "visible": True, "no_activate": True}
    assert out["pos"] == [200, 150]  # a lock toggle re-shows the window in place


def test_overlay_never_takes_focus_when_shown(tmp_path):
    _tr, ov, _w = make(tmp_path)
    assert ov.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    ov.set_locked(True)
    ov.set_locked(False)
    assert ov.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_show_hide_and_pause_actions(tmp_path):
    tr, ov, w = make(tmp_path)
    ov.show()
    ov.toggle_visible()
    assert not ov.isVisible()
    ov.toggle_visible()
    assert ov.isVisible()
    was = tr.paused
    w.toggle_pause()
    assert tr.paused is not was


def test_menu_shows_the_configured_hotkeys(tmp_path):
    _tr, _ov, w = make(tmp_path)
    assert w.lock_action.text().endswith("\tCtrl+Shift+F9")
    w.tracker.settings.hotkeys["lock"] = ""
    w._label_hotkeys()
    assert "\t" not in w.lock_action.text()
