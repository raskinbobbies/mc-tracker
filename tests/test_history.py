import json

from mctracker import history
from mctracker.kills import Mob
from mctracker.session import Tracker

HUNT = [Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98)]


def grind(tr, map_name, exp_values):
    tr.on_map(map_name, HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    total = 1000
    for i, v in enumerate(exp_values, start=1):
        tr.on_message_lines([(f"You received EXP (+{v})", i * 22.0)], now=i)
        total += v
        tr.on_status(45, total, 10.0 + i / 10, now=i)
    tr.on_map("Town", [])


def test_saved_sessions_load_newest_first_with_ids(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    grind(tr, "Primeval Forest I", [82, 98, 98])
    sessions = history.load(tmp_path)
    assert len(sessions) == 1
    s = sessions[0]
    assert s.map == "Primeval Forest I"
    assert s.exp == 278
    assert s.kills == 3
    kills = {e.name: (e.id, e.count) for e in s.entries("kill")}
    assert kills == {"Aqumander": (57, 2), "Glowshroom": (54, 1)}


def test_legacy_files_without_ids(tmp_path):
    (tmp_path / "20261125-010000.json").write_text(json.dumps({
        "map": "Old", "started_at": "2026-11-25T01:00:00", "active_seconds": 60,
        "exp_gained": 600, "party_kills": 5, "kills": {"Snail": 5},
    }))
    (tmp_path / "broken.json").write_text("{not json")
    s = history.load(tmp_path)
    assert len(s) == 1
    assert s[0].entries("kill") == [history.Entry(None, "Snail", 5)]
    assert s[0].exp_per_min == 600


def test_day_totals(tmp_path):
    for name, day, exp, secs in [("a", "2026-11-24T10:00:00", 100, 60), ("b", "2026-11-25T10:00:00", 200, 60),
                                 ("c", "2026-11-25T12:00:00", 400, 120)]:
        (tmp_path / f"{name}.json").write_text(json.dumps(
            {"map": name, "started_at": day, "active_seconds": secs, "exp_gained": exp, "party_kills": 1}))
    days = history.by_day(history.load(tmp_path))
    assert [(d.day, d.sessions, d.exp, d.seconds) for d in days] == [
        ("2026-11-25", 2, 600, 180), ("2026-11-24", 1, 100, 60)]
    assert days[0].exp_per_min == 200
