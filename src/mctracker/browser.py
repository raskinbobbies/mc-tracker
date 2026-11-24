"""Open mesos.watch in the user's own browser, reusing an existing tab.

mesos.watch blocks embedded browsers and has no per-item URLs, so a price
check copies the item name to the clipboard and brings up mesos.watch:

1. a browser window whose active tab is mesos.watch -> bring it to front
2. a browser tab named mesos.watch (Chromium browsers / Firefox, found via
   Windows UI Automation) -> select it and bring its window to front
3. otherwise open https://mesos.watch/ in the default browser
"""

from __future__ import annotations

import ctypes
import os
import webbrowser
from ctypes import wintypes

MESOS_WATCH_URL = "https://mesos.watch/"
TAB_MARKER = "mesos.watch"
BROWSER_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass")
# Electron apps (Discord, Spotify, ...) share Chrome's window class, so match the exe too.
BROWSER_EXES = {"chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe", "firefox.exe", "arc.exe"}


def _exe_name(pid: int) -> str:
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        k32.CloseHandle(h)


def _browser_hwnds() -> list[int]:
    """Visible top-level browser windows (plain Win32: UIA's root walk is slow)."""
    user32 = ctypes.windll.user32
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in BROWSER_CLASSES:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _exe_name(pid.value) in BROWSER_EXES:
                out.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


def _browser_windows():
    import uiautomation as auto

    for hwnd in _browser_hwnds():
        yield auto.ControlFromHandle(hwnd)


def _find_tab(window, depth: int = 0):
    """The mesos.watch tab in this browser window, if any. Skips page content."""
    import uiautomation as auto

    if depth > 14:
        return None
    for c in window.GetChildren():
        ct = c.ControlType
        if ct == auto.ControlType.DocumentControl:
            continue
        if ct == auto.ControlType.TabItemControl:
            if TAB_MARKER in (c.Name or ""):
                return c
            continue
        if (found := _find_tab(c, depth + 1)) is not None:
            return found
    return None


def _select_tab(tab) -> bool:
    for pattern in ("GetSelectionItemPattern", "GetLegacyIAccessiblePattern"):
        try:
            p = getattr(tab, pattern)()
            if p is None:
                continue
            if pattern == "GetSelectionItemPattern":
                p.Select()
            else:
                p.DoDefaultAction()
            return True
        except Exception:
            continue
    return False


def focus_mesos_watch() -> str:
    """Bring mesos.watch to the front. Returns what happened, for the status bar."""
    try:
        windows = list(_browser_windows())
        # Active tab already mesos.watch: the window title says so.
        for w in windows:
            if TAB_MARKER in (w.Name or ""):
                w.SetActive()
                w.SetFocus()
                return "Switched to your mesos.watch tab"
        for w in windows:
            tab = _find_tab(w)
            if tab is not None and _select_tab(tab):
                w.SetActive()
                w.SetFocus()
                return "Switched to your mesos.watch tab"
    except Exception:
        pass  # UI Automation unavailable or the browser refused: just open it
    webbrowser.open(MESOS_WATCH_URL)
    return "Opened mesos.watch"
