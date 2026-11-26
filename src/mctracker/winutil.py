"""Small Win32 helpers: process names and finding top-level windows."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def exe_name(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def top_level_windows(visible_only: bool = True) -> list[int]:
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not visible_only or user32.IsWindowVisible(hwnd):
            out.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


def window_exists(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def window_area(hwnd: int) -> int:
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return 0
    return max(0, r.right - r.left) * max(0, r.bottom - r.top)


def find_game_window(exe: str = "maplestory.exe", title: str = "MapleStory") -> int | None:
    """The game's main window: owned by the game exe, else an exact title match.

    Never a substring title match: other windows (browser tabs, chat apps,
    editors) often have "MapleStory" somewhere in their title.
    """
    exe = exe.lower()
    owned, by_title = [], None
    for hwnd in top_level_windows():
        if exe_name(window_pid(hwnd)) == exe and window_title(hwnd):
            owned.append(hwnd)
        elif by_title is None and window_title(hwnd) == title:
            by_title = hwnd
    if owned:  # the game may also own popups/notices: take the biggest window
        return max(owned, key=window_area)
    return by_title


def foreground_is_game(exe: str = "maplestory.exe", title: str = "MapleStory") -> bool:
    """Whether the foreground window is the game's (owned by the game exe, or an exact title match)."""
    hwnd = user32.GetForegroundWindow()
    return bool(hwnd) and (exe_name(window_pid(hwnd)) == exe.lower() or window_title(hwnd) == title)
