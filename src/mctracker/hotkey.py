"""Global hotkey (Win32 RegisterHotKey) and cursor position helpers."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

user32 = ctypes.windll.user32

MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
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


class GlobalHotkey(QObject):
    pressed = Signal()
    failed = Signal(str)

    def __init__(self, spec: str, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._thread_id: int | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="hotkey", daemon=True).start()

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        try:
            mods, vk = parse_hotkey(self.spec)
        except ValueError as e:
            self.failed.emit(str(e))
            return
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
            self.failed.emit(f"Hotkey {self.spec} is already in use")
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.pressed.emit()
        finally:
            user32.UnregisterHotKey(None, 1)


def cursor_in_window(hwnd: int | None) -> tuple[int, int] | None:
    """Cursor position in the game window's client coordinates."""
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    if hwnd:
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return pt.x, pt.y
