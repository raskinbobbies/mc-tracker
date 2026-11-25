import json

from mctracker import history
from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.kills import Mob
from mctracker.session import Tracker

HUNT = [Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98)]


def grind(tr, map_name, exp_values):
    tr.on_map(map_name, HUNT)
    total = 100_000
    tr.on_status(45, total, bar_percent(total, EXP_TABLE[45]), now=0)
    screen = []  # the message area: older lines stay visible as new ones arrive
    for i, v in enumerate(exp_values, start=1):
        screen = screen[-4:] + [f"You received EXP (+{v})"]
        tr.on_message_lines([(t, j * 22.0) for j, t in enumerate(screen)], now=i)
        total += v
        tr.on_status(45, total, bar_percent(total, EXP_TABLE[45]), now=i)
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


def test_non_session_json_is_skipped(tmp_path):
    (tmp_path / "list.json").write_text("[1, 2]")
    (tmp_path / "null.json").write_text('{"started_at": null}')
    (tmp_path / "broken.json").write_text("{nope")
    out = history.load(tmp_path)
    assert [s.path.name for s in out] == ["null.json"]  # dated by file mtime


def _saved(tmp_path, name, **data):
    (tmp_path / f"{name}.json").write_text(json.dumps({"started_at": "2026-11-25T10:00:00", **data}))
    return history.load(tmp_path)[0]


def test_net_mesos_per_min(tmp_path):
    s = _saved(tmp_path, "a", active_seconds=1200, mesos=15_000, mesos_spent=3_000)
    assert s.net_mesos == 12_000
    assert s.net_mesos_per_min == 600


def test_net_mesos_per_min_without_time_or_fields(tmp_path):
    assert _saved(tmp_path, "a").net_mesos_per_min == 0.0  # old file: no mesos, no seconds


def test_potions_sum_used_list(tmp_path):
    s = _saved(tmp_path, "a", active_seconds=600, items_used_estimate=True,
               used_list=[{"id": 1, "name": "Red Potion", "count": 30},
                          {"id": None, "name": "Other", "count": 20}])
    assert s.potions == 50
    assert s.potions_per_min == 5


def test_potions_from_legacy_name_dict(tmp_path):
    s = _saved(tmp_path, "a", active_seconds=60, used={"Red Potion": 4, "Blue Potion": 2})
    assert s.potions == 6


def test_potions_none_without_items_used_data(tmp_path):
    s = _saved(tmp_path, "a", active_seconds=60, exp_gained=5)
    assert s.potions is None and s.potions_per_min is None


def test_potions_zero_when_estimate_on_but_nothing_used(tmp_path):
    s = _saved(tmp_path, "a", active_seconds=60, items_used_estimate=True, used={}, used_list=[])
    assert s.potions == 0 and s.potions_per_min == 0
