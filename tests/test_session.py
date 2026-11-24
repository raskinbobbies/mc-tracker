import json

from mctracker.kills import Mob
from mctracker.session import Tracker

HUNT = [Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98), Mob(58, "Echopus", 107)]
EXP = "You received EXP (+{})"


def make(tmp_path):
    return Tracker(save_dir=tmp_path, item_resolver={"Echopus Leg": 4000055}.get)


def test_town_never_starts_a_session(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Forgotten Hollow", [])
    tr.on_status(45, 100, 1.0, now=0)
    tr.on_status(45, 500, 2.0, now=1)  # quest reward in town
    tr.on_message_lines([(EXP.format(400), 0)], now=1)
    assert tr.state == "waiting" and tr.session is None


def test_first_exp_starts_session_on_hunting_map(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    assert tr.session is None
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    assert tr.state == "active"
    tr.on_status(45, 1082, 10.8, now=2)
    assert tr.session.exp.gained == 82
    assert tr.session.kills == {54: 1}


def test_leaving_map_saves_session_and_waits(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    tr.on_status(45, 1082, 10.8, now=2)
    tr.on_map("Forgotten Hollow", [])
    assert tr.session is None and len(tr.saved) == 1
    rec = json.loads(tr.saved[0].read_text())
    assert rec["map"] == "Primeval Forest I"
    assert rec["exp_gained"] == 82
    assert rec["kills"] == {"Glowshroom": 1}
    assert tr.today()["exp"] == 82


def test_today_totals_survive_restart(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    tr.on_status(45, 1082, 10.8, now=2)
    tr.restart()
    assert make(tmp_path).today()["exp"] == 82  # reloaded from disk


def test_idle_time_does_not_count(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_status(45, 1100, 11.0, now=10)  # gain -> session starts
    tr.on_status(45, 1200, 12.0, now=20)
    s = tr.session
    assert s.active_s == 10
    s.tick(now=500)  # AFK for 8 minutes
    assert s.is_idle(now=500)
    tr.on_status(45, 1300, 13.0, now=510)
    assert s.active_s <= 70  # only up to the idle threshold, not 490 s


def test_pause_ignores_gains(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    tr.on_status(45, 1082, 10.8, now=2)
    tr.pause()
    tr.on_message_lines([(EXP.format(82), 0), (EXP.format(98), 22)], now=3)
    tr.on_status(45, 1500, 15.0, now=4)
    tr.resume()
    tr.on_status(45, 1500, 15.0, now=5)  # new baseline
    tr.on_status(45, 1600, 16.0, now=6)
    assert tr.session.exp.gained == 82 + 100
    assert tr.session.total_kills == 1


def test_party_screenshot_sequence(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_message_lines([
        ("Bonus EXP for PARTY (+6)", 770), ("You received EXP (+33)", 792),
        ("Bonus EXP for PARTY (+3)", 814), ("You received EXP (+64)", 836),
        ("Bonus EXP for PARTY (+6)", 858), ("Echopus Leg x1 earned. (Etc)", 880),
    ], now=0)
    s = tr.session
    assert s.total_kills == 2
    assert s.message_exp == 33 + 3 + 64 + 6
    assert s.loot == {4000055: 1}


def test_exp_per_minute_uses_active_clock(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    s = tr.session
    for i, now in enumerate(range(2, 130, 2)):  # 100 EXP every 2 s for ~2 min
        tr.on_status(45, 1082 + 100 * i, 10.8, now=now)
    mins = s.exp_per_minute
    assert len(mins) == 3 and sum(mins) == s.exp.gained
    assert s.best_minute() == max(mins) > 0
    assert s.to_dict()["exp_per_minute"] == mins


def test_idle_gap_adds_no_minutes(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    tr.on_status(45, 1100, 10.8, now=2)
    tr.on_status(45, 1200, 10.9, now=1000)  # long idle gap, then gain
    assert len(tr.session.exp_per_minute) <= 2
