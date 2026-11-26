"""Global hotkeys (Win32 RegisterHotKey) and cursor position helpers.

Hotkeys are RegisterHotKey only: Windows tells us when the combination is pressed. No keyboard hook, no
input sent anywhere, nothing touches the game's process (the app captures passively; see docs/decisions.md).
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QObject, Signal

from .items import HOTKEY_LABELS

user32 = ctypes.windll.user32

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PRICE_HOTKEY = "alt+a"  # app.py's --hotkey default
NAMED_KEYS = {**{f"f{i}": 0x6F + i for i in range(1, 13)}, "space": 0x20, "tab": 0x09}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+shift+p' -> (modifiers, virtual key code)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            raise ValueError(f"unknown modifier {p!r}")
        mods |= MODS[p]
    key = parts[-1]
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise ValueError(f"unknown key {key!r}")
    return mods, vk


def same_hotkey(a: str, b: str) -> bool:
    """True when two specs name the same combination ('Shift+Ctrl+F9' == 'ctrl+shift+f9')."""
    try:
        return parse_hotkey(a) == parse_hotkey(b)
    except ValueError:
        return False


def hotkey_problem(specs: dict[str, str], reserved: str = PRICE_HOTKEY) -> str:
    """Why these hotkeys can't be used together ("" = fine). ``reserved`` is the price-check hotkey."""
    seen: dict[str, str] = {}
    for name, spec in specs.items():
        if not spec:
            continue
        try:
            parse_hotkey(spec)
        except ValueError as e:
            return f"{spec}: {e}"
        if not spec.split("+")[:-1]:
            return f"{spec}: add a modifier (ctrl, alt, shift or win), or the game loses that key"
        if reserved and same_hotkey(spec, reserved):
            return f"{spec} is the price-check hotkey"
        for other, other_spec in seen.items():
            if same_hotkey(spec, other_spec):
                return f"{spec} is used twice ({HOTKEY_LABELS.get(other, other)})"
        seen[name] = spec
    return ""


class GlobalHotkey(QObject):
    """One RegisterHotKey on its own thread (a thread-owned hotkey needs that thread's message loop).
    ``pressed`` and ``failed`` are emitted from that thread: connect them to a QObject's method so Qt
    queues them to the GUI thread."""

    pressed = Signal()
    failed = Signal(str)

    def __init__(self, spec: str, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()  # set once registered (or failed): stop() can post its WM_QUIT then

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Unregister and end the thread. Waits for it, so the same combination can be registered again."""
        self.request_stop(timeout)
        self.join(timeout)

    def request_stop(self, timeout: float = 1.0) -> None:
        """Ask the thread to unregister and end (once it has registered, or given up), without waiting for it."""
        self._ready.wait(max(0.0, timeout))
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def join(self, timeout: float = 1.0) -> None:
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(max(0.0, timeout))

    def _run(self) -> None:
        try:
            try:
                mods, vk = parse_hotkey(self.spec)
            except ValueError as e:
                self.failed.emit(f"Hotkey {self.spec}: {e}")
                return
            if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
                self.failed.emit(f"Hotkey {self.spec} is already in use")
                return
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()  # the queue exists once registered
        finally:
            self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.pressed.emit()
        finally:
            user32.UnregisterHotKey(None, 1)


class HotkeySet(QObject):
    """Named hotkeys ({"lock": "ctrl+shift+f9", ...}) mapped to actions, run on the GUI thread.

    ``apply`` swaps in new combinations (an empty one is off). A combination that can't be registered
    (taken by another program, or not a key we know) is reported through ``failed`` and skipped; the
    others still work.
    """

    failed = Signal(str)

    def __init__(self, actions: dict[str, Callable[[], None]], parent=None, factory=GlobalHotkey):
        super().__init__(parent)
        self.actions = actions
        self._factory = factory
        self._keys: dict[str, GlobalHotkey] = {}

    def apply(self, specs: dict[str, str]) -> None:
        self.stop()
        for name, spec in specs.items():
            if not spec or name not in self.actions:
                continue
            hk = self._keys[name] = self._factory(spec, self)
            hk.pressed.connect(self._fire)
            hk.failed.connect(self._fail)
            hk.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Tell every thread to stop, then wait for them: about ``timeout`` at worst in all, not per hotkey."""
        end = time.monotonic() + timeout
        keys = list(self._keys.values())
        for hk in keys:
            hk.request_stop(end - time.monotonic())
        for hk in keys:
            hk.join(end - time.monotonic())
            hk.deleteLater()
        self._keys = {}

    def _fire(self) -> None:
        for name, hk in self._keys.items():
            if hk is self.sender():
                self.actions[name]()

    def _fail(self, msg: str) -> None:
        self.failed.emit(msg)


def cursor_in_window(hwnd: int | None) -> tuple[int, int] | None:
    """Cursor position in the game window's client coordinates."""
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    if hwnd:
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y
