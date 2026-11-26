"""Small Win32 helpers: process names and finding top-level windows (passive: no process handles)."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # our own: its argtypes don't touch windll's

# Passive only: nothing here may open a handle to another process (not even a query-only one, and
# never its memory). Anti-cheats log handles opened to the game. Process names come from
# a Toolhelp snapshot, which lists every process without opening any of them.
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
SNAPSHOT_TTL_S = 2.0


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),  # ULONG_PTR
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_snapshot: tuple[float, dict[int, str]] | None = None  # (taken at, pid -> lower-case exe name)


def process_names() -> dict[int, str]:
    """pid -> lower-case exe name for every running process, from one Toolhelp snapshot.

    Opens no process handle (only the snapshot's own). Cached for ``SNAPSHOT_TTL_S``.
    """
    global _snapshot
    now = time.monotonic()
    if _snapshot is not None and now - _snapshot[0] < SNAPSHOT_TTL_S:
        return _snapshot[1]
    names: dict[int, str] = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap and snap != INVALID_HANDLE_VALUE:
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                names[int(entry.th32ProcessID)] = entry.szExeFile.lower()
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
    _snapshot = (now, names)
    return names


def exe_name(pid: int, names: dict[int, str] | None = None) -> str:
    """The lower-case exe name of ``pid`` ("" if unknown), without opening the process."""
    return (process_names() if names is None else names).get(pid, "")


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


_game: tuple[tuple[str, str], int, int, str] | None = None  # ((exe, title) asked), hwnd, pid, its title


def _cached_game(exe: str, title: str) -> int | None:
    """The game window found before for this (exe, title), if it is still that window and still visible:
    no enumeration, no snapshot. A hidden one (a splash, a mode switch) is searched for again."""
    global _game
    if _game is None:
        return None
    key, hwnd, pid, seen = _game
    t = window_title(hwnd) if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) else ""
    if key == (exe, title) and t and t == seen and window_pid(hwnd) == pid:
        return hwnd
    _game = None
    return None


def forget_game() -> None:
    """Drop the tracked game window: the next ``find_game_window`` searches afresh (a capture start)."""
    global _game
    _game = None


def find_game_window(exe: str = "maplestory.exe", title: str = "MapleStory") -> int | None:
    """The game's main window: owned by the game exe, else an exact title match.

    Never a substring title match: other windows (browser tabs, chat apps,
    editors) often have "MapleStory" somewhere in their title. Once found, the
    window is tracked by hwnd (IsWindow, visible, same pid and title); the windows are
    only enumerated again once it is gone.
    """
    global _game
    exe = exe.lower()
    if (hwnd := _cached_game(exe, title)) is not None:
        return hwnd
    names = process_names()  # one snapshot for all windows
    owned, by_title = [], None
    for hwnd in top_level_windows():
        t = window_title(hwnd)
        if t and exe_name(window_pid(hwnd), names) == exe:
            owned.append(hwnd)
        elif by_title is None and t == title:
            by_title = hwnd
    found = max(owned, key=window_area) if owned else by_title  # the game may own popups: take the biggest
    if found is not None:
        _game = ((exe, title), found, window_pid(found), window_title(found))
    return found


def foreground_is_game(exe: str = "maplestory.exe", title: str = "MapleStory") -> bool:
    """Whether the foreground window is the game's (its window, another of its process's windows, or an
    exact title match). Compares against the tracked game hwnd; looks for the game only when it is gone."""
    fg = user32.GetForegroundWindow()
    if not fg:
        return False
    game = find_game_window(exe, title)
    if game is not None and (fg == game or window_pid(fg) == window_pid(game)):
        return True
    return window_title(fg) == title
