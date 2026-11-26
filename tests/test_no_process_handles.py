"""Passive only: no code path opens a handle to another process (see docs/decisions.md).

kernel32/user32 are faked: OpenProcess (and friends) fail the test if anything calls them.
"""

from __future__ import annotations

import pytest

from mctracker import browser, winutil


class FakeKernel32:
    def __init__(self, procs: dict[int, str]):
        self.procs = procs
        self.snapshots = 0
        self.closed: list[int] = []
        self._it = None

    def __getattr__(self, name):  # OpenProcess, ReadProcessMemory, QueryFullProcessImageNameW, ...
        raise AssertionError(f"kernel32.{name} called: no process handles allowed")

    def CreateToolhelp32Snapshot(self, flags, pid):
        assert flags == winutil.TH32CS_SNAPPROCESS and pid == 0
        self.snapshots += 1
        self._it = iter(self.procs.items())
        return 1234

    def _next(self, snap, ref):
        assert snap == 1234
        entry = ref._obj
        assert entry.dwSize == winutil.ctypes.sizeof(winutil.PROCESSENTRY32W)
        try:
            pid, exe = next(self._it)
        except StopIteration:
            return 0
        entry.th32ProcessID, entry.szExeFile = pid, exe
        return 1

    Process32FirstW = Process32NextW = _next

    def CloseHandle(self, h):
        self.closed.append(h)
        return 1


class FakeUser32:
    def __init__(self, windows: dict[int, tuple[int, str, str, int]]):
        self.windows = windows  # hwnd -> (pid, title, class, area)
        self.enumerations = 0
        self.foreground = 0

    def EnumWindows(self, cb, _):
        self.enumerations += 1
        for h in list(self.windows):
            cb(h, 0)
        return 1

    def IsWindowVisible(self, h):
        return h in self.windows

    def IsWindow(self, h):
        return h in self.windows

    def GetWindowTextLengthW(self, h):
        return len(self.windows.get(h, (0, ""))[1])

    def GetWindowTextW(self, h, buf, n):
        buf.value = self.windows.get(h, (0, ""))[1][: n - 1]
        return len(buf.value)

    def GetClassNameW(self, h, buf, n):
        buf.value = self.windows[h][2]
        return len(buf.value)

    def GetWindowThreadProcessId(self, h, ref):
        ref._obj.value = self.windows.get(h, (0,))[0]
        return 1

    def GetWindowRect(self, h, ref):
        ref._obj.right = ref._obj.bottom = int(self.windows[h][3] ** 0.5)
        return 1

    def GetForegroundWindow(self):
        return self.foreground


PROCS = {4: "System", 100: "MapleStory.exe", 200: "chrome.exe", 300: "Discord.exe", 400: "notepad.exe"}
WINDOWS = {
    11: (100, "MapleStory", "MapleStoryClass", 800 * 600),
    12: (100, "Notice", "#32770", 200 * 100),  # a game popup
    21: (200, "mesos.watch - Google Chrome", "Chrome_WidgetWin_1", 1000),
    31: (300, "Discord", "Chrome_WidgetWin_1", 1000),  # Electron: Chrome's class, not a browser
    41: (400, "MapleStory guide.txt - Notepad", "Notepad", 1000),  # substring title: not the game
}


@pytest.fixture
def fake(monkeypatch):
    k, u = FakeKernel32(dict(PROCS)), FakeUser32(dict(WINDOWS))
    monkeypatch.setattr(winutil, "kernel32", k)
    monkeypatch.setattr(winutil, "user32", u)
    monkeypatch.setattr(winutil, "_snapshot", None)
    monkeypatch.setattr(winutil, "_game", None)
    clock = [1000.0]
    monkeypatch.setattr(winutil.time, "monotonic", lambda: clock[0])
    return k, u, clock


def test_snapshot_parses_lowercase_names_and_closes_the_snapshot(fake):
    k, _u, _clock = fake
    names = winutil.process_names()
    assert names == {4: "system", 100: "maplestory.exe", 200: "chrome.exe", 300: "discord.exe", 400: "notepad.exe"}
    assert k.closed == [1234]
    assert winutil.exe_name(100) == "maplestory.exe" and winutil.exe_name(999) == ""


def test_snapshot_is_cached_briefly(fake):
    k, _u, clock = fake
    winutil.process_names()
    winutil.exe_name(200)
    clock[0] += winutil.SNAPSHOT_TTL_S - 0.1
    winutil.process_names()
    assert k.snapshots == 1
    clock[0] += 0.2
    winutil.process_names()
    assert k.snapshots == 2


def test_failed_snapshot_gives_no_names(fake, monkeypatch):
    k, _u, _clock = fake
    monkeypatch.setattr(k, "CreateToolhelp32Snapshot", lambda *_a: winutil.INVALID_HANDLE_VALUE, raising=False)
    assert winutil.process_names() == {}
    assert k.closed == []


def test_find_game_window_takes_one_snapshot_and_the_biggest_game_window(fake):
    k, _u, _clock = fake
    assert winutil.find_game_window() == 11
    assert k.snapshots == 1  # one per call, not one per window


def test_game_hwnd_is_tracked_without_enumerating_again(fake):
    k, u, clock = fake
    assert winutil.find_game_window() == 11
    clock[0] += 60  # well past the snapshot cache
    for _ in range(5):
        assert winutil.find_game_window() == 11
    assert u.enumerations == 1 and k.snapshots == 1


def test_game_closing_falls_back_to_a_fresh_find(fake):
    k, u, clock = fake
    assert winutil.find_game_window() == 11
    del u.windows[11], u.windows[12]
    clock[0] += 60
    assert winutil.find_game_window() is None
    assert u.enumerations == 2
    # A reused hwnd from another process is not the game.
    u.windows[11] = (400, "MapleStory", "Other", 10)
    winutil._game = (11, 100, "MapleStory")
    assert winutil._cached_game("MapleStory") is None


def test_exact_title_match_without_the_exe(fake):
    _k, u, _clock = fake
    del u.windows[11], u.windows[12]
    u.windows[51] = (500, "MapleStory", "X", 10)  # 500 isn't in the snapshot
    assert winutil.find_game_window() == 51


def test_foreground_is_game_uses_the_tracked_hwnd(fake):
    k, u, clock = fake
    u.foreground = 11
    assert winutil.foreground_is_game()
    clock[0] += 60
    u.foreground = 12  # the game's popup
    assert winutil.foreground_is_game()
    u.foreground = 41
    assert not winutil.foreground_is_game()
    u.foreground = 0
    assert not winutil.foreground_is_game()
    assert u.enumerations == 1 and k.snapshots == 1


def test_browser_lookup_opens_no_process(fake):
    k, _u, _clock = fake
    assert browser._browser_hwnds() == [21]
    assert k.snapshots == 1


def test_no_source_file_opens_a_process():
    from pathlib import Path

    banned = ("OpenProcess", "ReadProcessMemory", "QueryFullProcessImageName", "psutil", "OpenThread",
              "DuplicateHandle", "GetModuleFileNameEx", "EnumProcess")
    src = Path(winutil.__file__).parent
    hits = [f"{p.name}: {b}" for p in src.rglob("*.py") for b in banned if b in p.read_text(encoding="utf-8")]
    assert hits == []
