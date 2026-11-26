"""The fixed install folder: staging a new build, swapping it in (install.ps1), and the running install
handing over to it (``follow.start_install``, ``follow.SwapWatch``). Only temp folders; no registry."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mctracker import follow

SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "install.ps1"


def _build(folder: Path, tag: str) -> Path:
    """A fake one-folder build: the exe, a data file and the bundled install script."""
    (folder / "_internal").mkdir(parents=True)
    (folder / "mc-tracker.exe").write_text(tag, encoding="utf-8")
    (folder / "_internal" / "data.txt").write_text(tag, encoding="utf-8")
    shutil.copy(SCRIPT, folder / "_internal" / "install.ps1")
    return folder / "mc-tracker.exe"


def test_build_stamp_reads_build_txt(tmp_path):
    exe = _build(tmp_path / "b", "x")
    assert follow.build_stamp(exe) == ""
    exe.with_name("build.txt").write_text("20261126T100000Z abc\r\n", encoding="utf-8")
    assert follow.build_stamp(exe) == "20261126T100000Z abc"


def test_staged_update_is_the_next_folder_with_an_exe(tmp_path):
    exe = _build(tmp_path / "mc-tracker", "old")
    assert follow.staged_update(exe) is None
    (tmp_path / "mc-tracker.next").mkdir()
    assert follow.staged_update(exe) is None  # a half-copied folder is not a build
    _build(tmp_path / "other.next", "new")
    assert follow.staged_update(exe) is None
    shutil.rmtree(tmp_path / "mc-tracker.next")
    _build(tmp_path / "mc-tracker.next", "new")
    assert follow.staged_update(exe) == tmp_path / "mc-tracker.next"


def test_start_install_runs_a_temp_copy_of_the_staged_script(tmp_path):
    exe = _build(tmp_path / "mc-tracker", "old")
    spawned = []
    assert follow.start_install(exe, "--follow-game", spawn=spawned.append) is False  # nothing staged
    staged = _build(tmp_path / "mc-tracker.next", "new").parent
    (staged / "_internal" / "install.ps1").write_text("# the new build's script", encoding="utf-8")
    assert follow.start_install(exe, "--follow-game", spawn=lambda c: spawned.append(c) or True) is True
    cmd = spawned[-1]
    script = Path(cmd[cmd.index("-File") + 1])
    try:
        assert script.parent != staged and script.read_text(encoding="utf-8") == "# the new build's script"
        assert cmd[cmd.index("-InstallDir") + 1] == str(exe.parent)
        assert cmd[-3:] == ["-Start", "-Arguments", "--follow-game"]
    finally:
        script.unlink()
    assert follow.install_command(script, exe.parent, None)[-2:] == ["-WaitSeconds", str(follow.SWAP_WAIT_S)]
    assert follow.install_command(script, exe.parent, "")[-1] == " "  # still an argument: start with none


def test_the_waiter_swaps_only_when_the_game_and_app_are_away(tmp_path):
    exe = _build(tmp_path / "mc-tracker", "old")
    state = {"game": False, "app": False, "ok": True}
    calls = []
    swap = follow.SwapWatch(exe, "--follow-game", lambda: state["game"], lambda: state["app"],
                            lambda: calls.append("quit"),
                            start=lambda e, a: calls.append((e, a)) or state["ok"])
    assert swap.check() is False and calls == []  # nothing staged
    _build(tmp_path / "mc-tracker.next", "new")
    state["game"] = True
    assert swap.check() is False
    state["game"], state["app"] = False, True
    assert swap.check() is False and calls == []
    state["app"] = False
    assert swap.check() is True and calls == [(exe, "--follow-game"), "quit"]
    assert swap.check() is False and len(calls) == 2  # once


def test_a_swap_that_cannot_start_keeps_the_waiter(tmp_path):
    exe = _build(tmp_path / "mc-tracker", "old")
    _build(tmp_path / "mc-tracker.next", "new")
    quits = []
    swap = follow.SwapWatch(exe, "", lambda: False, lambda: False, lambda: quits.append(1), start=lambda e, a: False)
    assert swap.check() is False and swap.check() is False and quits == []

    def boom():
        raise OSError("probe")

    assert follow.SwapWatch(exe, "", boom, lambda: False, lambda: quits.append(1)).check() is False
    assert follow.SwapWatch(None, "", lambda: False, lambda: False, lambda: quits.append(1)).check() is False
    assert quits == []


# --- install.ps1, against temp folders ---------------------------------------------

needs_powershell = pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell.exe") is None,
                                      reason="install.ps1 runs on Windows PowerShell")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


@needs_powershell
def test_install_stages_a_build_and_swaps_it_in(tmp_path):
    install = tmp_path / "app" / "mc-tracker"
    build = _build(tmp_path / "dist" / "mc-tracker", "v1").parent
    r = _run("-BuildDir", str(build), "-InstallDir", str(install))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (install / "mc-tracker.exe").read_text(encoding="utf-8") == "v1"  # first install: no old folder
    (build / "mc-tracker.exe").write_text("v2", encoding="utf-8")
    (build / "_internal" / "data.txt").write_text("v2", encoding="utf-8")
    r = _run("-BuildDir", str(build), "-InstallDir", str(install))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (install / "_internal" / "data.txt").read_text(encoding="utf-8") == "v2"
    assert sorted(p.name for p in install.parent.iterdir()) == ["mc-tracker"]  # no .next, no .old-* left
    assert (build / "mc-tracker.exe").exists()  # the build folder itself is copied, not moved


@needs_powershell
def test_install_leaves_the_build_staged_while_the_installed_exe_runs(tmp_path):
    install = tmp_path / "app" / "mc-tracker"
    _build(install, "v1")
    build = _build(tmp_path / "dist" / "mc-tracker", "v2").parent
    with open(install / "mc-tracker.exe", "rb"):  # held open, as a running exe is
        r = _run("-BuildDir", str(build), "-InstallDir", str(install))
    assert r.returncode == 3, r.stdout + r.stderr
    assert (install / "mc-tracker.exe").read_text(encoding="utf-8") == "v1"
    assert (install.with_name("mc-tracker.next") / "mc-tracker.exe").read_text(encoding="utf-8") == "v2"
    r = _run("-InstallDir", str(install))  # the exe has exited: what the handover runs
    assert r.returncode == 0, r.stdout + r.stderr
    assert (install / "mc-tracker.exe").read_text(encoding="utf-8") == "v2"
    assert not install.with_name("mc-tracker.next").exists()


@needs_powershell
def test_install_with_nothing_staged_changes_nothing(tmp_path):
    install = tmp_path / "app" / "mc-tracker"
    _build(install, "v1")
    r = _run("-InstallDir", str(install))
    assert r.returncode == 0 and "Nothing staged" in r.stdout
    assert (install / "mc-tracker.exe").read_text(encoding="utf-8") == "v1"
