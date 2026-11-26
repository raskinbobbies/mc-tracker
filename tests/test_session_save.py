"""A session is never lost: autosave and recovery, failed saves, atomic writes, odd files on disk."""

import json
import os
from datetime import datetime, timezone

import pytest

from mctracker import history
from mctracker import session as session_mod
from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.kills import Mob
from mctracker.session import AUTOSAVE_S, CURRENT_FILE, SCHEMA_VERSION, Tracker

HUNT = [Mob(54, "Glowshroom", 82)]
MAP = "Primeval Forest I"
START = datetime(2026, 11, 26, 10, 0, 0)


def status(tr, exp, now, level=45):
    tr.on_status(level, exp, bar_percent(exp, EXP_TABLE[level]), now=now)


def running(tmp_path, started=START):
    """A tracker with a session that gained 100 EXP (clock 0..1)."""
    tr = Tracker(save_dir=tmp_path)
    tr.on_map(MAP, HUNT)
    status(tr, 100_000, now=0)
    status(tr, 100_100, now=1)
    assert tr.session is not None
    tr.session.started_at = started
    return tr


def run_for(tr, start, end):
    for t in range(start, end + 1):
        tr.tick(now=t)


def saved_files(d):
    return sorted(p.name for p in d.glob("*.json") if not p.name.startswith("."))


# --- autosave and recovery ------------------------------------------------------


def test_autosave_writes_the_running_session_and_a_normal_end_removes_it(tmp_path):
    tr = running(tmp_path)
    run_for(tr, 2, 1 + int(AUTOSAVE_S) - 1)
    assert not (tmp_path / CURRENT_FILE).exists()  # not before AUTOSAVE_S
    run_for(tr, 1 + int(AUTOSAVE_S), 2 + int(AUTOSAVE_S))
    rec = json.loads((tmp_path / CURRENT_FILE).read_text(encoding="utf-8"))
    assert rec["map"] == MAP and rec["exp_gained"] == 100 and rec["recovered"] is True
    assert history.load(tmp_path) == []  # the autosave is never shown as a saved session
    assert tr.today()["sessions"] == 1  # the running one, counted once
    path = tr.end_session()
    assert not (tmp_path / CURRENT_FILE).exists()
    assert "recovered" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_crash_is_recovered_once_on_the_next_start(tmp_path):
    tr = running(tmp_path, started=datetime.now().replace(microsecond=0))
    run_for(tr, 2, 400)
    del tr  # the app is killed: end_session never runs

    tr2 = Tracker(save_dir=tmp_path)
    before = tr2.today()["exp"]
    assert before == 0  # the autosave isn't a saved session yet
    msg = tr2.recover()
    assert msg == f"Recovered 6m on {MAP}."
    assert not (tmp_path / CURRENT_FILE).exists()
    assert len(saved_files(tmp_path)) == 1
    t = tr2.today()
    assert t["exp"] == 100 and t["sessions"] == 1  # counted once
    assert [s.map for s in history.load(tmp_path)] == [MAP]
    assert Tracker(save_dir=tmp_path).recover() is None  # nothing left to recover
    assert Tracker(save_dir=tmp_path).today()["sessions"] == 1


def test_recover_skips_a_session_that_was_already_saved(tmp_path):
    tr = running(tmp_path)
    run_for(tr, 2, 40)
    current = (tmp_path / CURRENT_FILE).read_text(encoding="utf-8")
    tr.end_session()
    (tmp_path / CURRENT_FILE).write_text(current, encoding="utf-8")  # stopped before removing it
    assert Tracker(save_dir=tmp_path).recover() is None
    assert len(saved_files(tmp_path)) == 1 and not (tmp_path / CURRENT_FILE).exists()


def test_a_garbled_autosave_is_moved_aside_and_never_stops_the_start(tmp_path):
    (tmp_path / CURRENT_FILE).write_text('{"map": "Ant Tu', encoding="utf-8")
    tr = Tracker(save_dir=tmp_path)
    assert tr.recover() is None
    assert not (tmp_path / CURRENT_FILE).exists()
    assert list(tmp_path.glob(".current.bad-*.json"))
    assert history.load(tmp_path) == []


# --- failed saves -----------------------------------------------------------------


def test_a_failed_save_keeps_the_session_and_retries(tmp_path, monkeypatch):
    tr = running(tmp_path)
    run_for(tr, 2, 40)  # autosaved
    real = session_mod.write_atomic
    fail = [True]

    def flaky(path, text):
        if fail[0] and not path.name.startswith("."):
            raise OSError(28, "No space left on device")
        real(path, text)

    monkeypatch.setattr(session_mod, "write_atomic", flaky)
    notices = []
    tr.on_notice.append(notices.append)
    assert tr.end_session() is None
    assert notices and "Couldn't save the session" in notices[0]
    assert saved_files(tmp_path) == []
    assert (tmp_path / CURRENT_FILE).exists()  # the autosave is kept as well, for a crash before the retry
    assert tr.last is not None and tr.last.exp.gained == 100

    run_for(tr, 41, 41 + int(AUTOSAVE_S))  # still failing: kept, no second notice
    assert len(tr._pending) == 1 and len(notices) == 1
    fail[0] = False
    run_for(tr, 42 + int(AUTOSAVE_S), 42 + 2 * int(AUTOSAVE_S))
    assert tr._pending == [] and len(saved_files(tmp_path)) == 1
    assert tr.today()["exp"] == (100 if START.date() == datetime.now().date() else 0)


def test_a_raising_on_saved_callback_never_stops_the_map_change(tmp_path):
    tr = running(tmp_path)

    def boom(_path):
        raise RuntimeError("UI gone")

    seen = []
    tr.on_saved += [boom, seen.append]
    tr.on_map("Ant Tunnel I", HUNT)
    assert tr.map_name == "Ant Tunnel I" and tr.session is None
    assert len(seen) == 1 and seen[0].exists()  # later callbacks still run


def test_session_writes_are_atomic(tmp_path, monkeypatch):
    tr = running(tmp_path)

    def no_replace(src, dst):
        raise OSError(13, "Access is denied")

    monkeypatch.setattr(os, "replace", no_replace)
    assert tr.end_session() is None
    assert list(tmp_path.iterdir()) == []  # no half-written file, no leftover temp file


def test_saved_file_has_a_schema_version(tmp_path):
    path = running(tmp_path).end_session()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_VERSION


def test_two_sessions_started_in_the_same_second_both_save(tmp_path):
    a = running(tmp_path).end_session()
    b = running(tmp_path).end_session()
    assert a != b and a.exists() and b.exists()
    assert b.name == f"{START:%Y%m%d-%H%M%S}-2.json"


# --- odd files on disk --------------------------------------------------------------


@pytest.mark.parametrize("content", ["[]", '"text"', "null", '{"exp_gained": null}', '{"exp_gained": "12"}',
                                     '{"exp_gained": "lots", "party_kills": [1], "active_seconds": 1e400}'])
def test_odd_session_files_never_stop_the_app_starting(tmp_path, content):
    today = datetime.now().strftime("%Y%m%d")
    (tmp_path / f"{today}-010000.json").write_text(content, encoding="utf-8")
    (tmp_path / f"{today}-020000.json").write_text('{"exp_gained": 50, "party_kills": 3}', encoding="utf-8")
    t = Tracker(save_dir=tmp_path).today()
    assert t["exp"] in (50, 62) and t["kills"] == 3


def test_history_coerces_odd_values(tmp_path):
    files = {
        "a.json": {"started_at": "2026-11-26T10:00:00+00:00", "active_seconds": None, "exp_gained": "600",
                   "loot_list": ["junk", {"id": 4000000, "name": "Snail Shell", "count": "3"}, {"id": "x"}]},
        "b.json": {"started_at": "2026-11-26T09:00:00", "active_seconds": 60, "exp_gained": 100,
                   "kills": ["not", "a", "dict"], "mesos": None},
        "c.json": {"started_at": 12, "map": ["x"], "active_seconds": "abc"},
        CURRENT_FILE: {"started_at": "2026-11-26T11:00:00", "exp_gained": 999},
    }
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    sessions = history.load(tmp_path)  # sorting naive and tz-aware start times must not raise
    assert sorted(s.path.name for s in sessions) == ["a.json", "b.json", "c.json"]
    a = next(s for s in sessions if s.path.name == "a.json")
    assert a.started.tzinfo is None
    assert a.started == datetime(2026, 11, 26, 10, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert a.seconds == 0 and a.exp == 600
    assert [(e.id, e.name, e.count) for e in a.entries("loot")] == [(4000000, "Snail Shell", 3), (None, "?", 0)]
    b = next(s for s in sessions if s.path.name == "b.json")
    assert b.entries("kill") == [] and b.mesos == 0
    c = next(s for s in sessions if s.path.name == "c.json")
    assert c.map == "Unknown map" and c.seconds == 0
    days = history.by_day(sessions)
    assert sum(d.sessions for d in days) == 3 and sum(d.seconds for d in days) == 60


# --- sleep / hibernate -------------------------------------------------------------


def test_a_suspend_gap_is_not_session_time(tmp_path):
    tr = running(tmp_path)
    run_for(tr, 2, 10)
    s = tr.session
    assert s.active_s == 9
    tr.tick(now=10 + 3600)  # the PC slept for an hour
    assert s.active_s == 9
    run_for(tr, 3611, 3620)  # the clock runs again after the wake-up
    assert s.active_s == 19


# --- review round: locked autosave, pending saves, --crash-test ---------------------

OLD = {"schema_version": 1, "map": "Old Map", "started_at": "2026-11-25T09:00:00", "active_seconds": 120,
       "exp_gained": 50, "recovered": True}


def test_a_locked_autosave_is_kept_never_overwritten_and_recovered_later(tmp_path, monkeypatch):
    (tmp_path / CURRENT_FILE).write_text(json.dumps(OLD), encoding="utf-8")
    real = session_mod.replace_file
    locked = [True]

    def replace(src, dst):
        if locked[0] and str(src).endswith(CURRENT_FILE):
            raise PermissionError(32, "The process cannot access the file")
        real(src, dst)

    monkeypatch.setattr(session_mod, "replace_file", replace)
    tr = Tracker(save_dir=tmp_path)
    notices = []
    tr.on_notice.append(notices.append)
    msg = tr.recover()
    assert msg and "Couldn't recover the last session yet" in msg
    assert json.loads((tmp_path / CURRENT_FILE).read_text(encoding="utf-8")) == OLD  # not moved aside
    assert not list(tmp_path.glob("*.bad-*"))

    tr.on_map(MAP, HUNT)
    status(tr, 100_000, now=0)
    status(tr, 100_100, now=1)
    run_for(tr, 2, 40)  # an autosave is due: it must not write over the last run's session
    assert json.loads((tmp_path / CURRENT_FILE).read_text(encoding="utf-8")) == OLD

    locked[0] = False
    run_for(tr, 41, 75)  # the next autosave recovers it first, then autosaves this session
    assert any(n.startswith("Recovered 2m on Old Map") for n in notices)
    assert [s.map for s in history.load(tmp_path)] == ["Old Map"]
    assert json.loads((tmp_path / CURRENT_FILE).read_text(encoding="utf-8"))["map"] == MAP
    tr.end_session()
    assert sorted(s.map for s in history.load(tmp_path)) == sorted(["Old Map", MAP])


def test_a_locked_autosave_survives_a_session_end(tmp_path, monkeypatch):
    (tmp_path / CURRENT_FILE).write_text(json.dumps(OLD), encoding="utf-8")

    def replace(src, dst):
        raise PermissionError(32, "locked")

    monkeypatch.setattr(session_mod, "replace_file", replace)
    tr = Tracker(save_dir=tmp_path)
    tr.recover()
    monkeypatch.undo()
    tr.on_map(MAP, HUNT)
    status(tr, 100_000, now=0)
    status(tr, 100_100, now=1)
    tr.end_session()  # a normal end removes the autosave: not the last run's
    assert json.loads((tmp_path / CURRENT_FILE).read_text(encoding="utf-8")) == OLD
    assert Tracker(save_dir=tmp_path).recover().startswith("Recovered 2m on Old Map")


def failing_saves(monkeypatch):
    real = session_mod.write_atomic
    fail = [True]

    def flaky(path, text):
        if fail[0] and not path.name.startswith("."):
            raise OSError(28, "No space left on device")
        real(path, text)

    monkeypatch.setattr(session_mod, "write_atomic", flaky)
    return fail


def test_a_failed_save_is_retried_on_quit_with_no_session_running(tmp_path, monkeypatch):
    fail = failing_saves(monkeypatch)
    tr = running(tmp_path)
    assert tr.end_session() is None
    assert len(list(tmp_path.glob(".pending-*.json"))) == 1  # kept on disk too
    fail[0] = False
    tr.end_session()  # what quitting runs: no session, but the pending one is saved
    assert tr._pending == [] and len(saved_files(tmp_path)) == 1
    assert not list(tmp_path.glob(".pending-*.json"))


def test_a_failed_save_survives_a_crash_and_is_recovered_once(tmp_path, monkeypatch):
    fail = failing_saves(monkeypatch)
    tr = running(tmp_path)
    run_for(tr, 2, 40)  # autosaved too
    assert tr.end_session() is None
    del tr  # killed before the retry
    fail[0] = False
    msg = Tracker(save_dir=tmp_path).recover()
    assert msg == f"Recovered 39s on {MAP}."  # the final record; the older autosave copy is dropped
    assert len(saved_files(tmp_path)) == 1
    assert not list(tmp_path.glob(".pending-*.json")) and not (tmp_path / CURRENT_FILE).exists()


def test_crash_test_and_demo_never_recover(tmp_path):
    from argparse import Namespace

    from mctracker.app import recover_on_start

    class Fake:
        called = 0

        def recover(self):
            self.called += 1
            return "Recovered"

    fake = Fake()
    assert recover_on_start(fake, Namespace(demo=False, crash_test=True)) is None
    assert recover_on_start(fake, Namespace(demo=True, crash_test=False)) is None
    assert fake.called == 0
    assert recover_on_start(fake, Namespace(demo=False, crash_test=False)) == "Recovered"


def test_autosave_failing_tells_the_user_once(tmp_path, monkeypatch):
    tr = running(tmp_path)
    notices = []
    tr.on_notice.append(notices.append)

    def broken(path, text):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(session_mod, "write_atomic", broken)
    run_for(tr, 2, 2 + 3 * int(AUTOSAVE_S))
    assert len(notices) == 1 and "Autosave failed" in notices[0]
