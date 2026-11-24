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


def test_one_off_exp_spike_does_not_inflate_best_minute_or_totals(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    tr.on_status(45, 5_000_000, 5.0, now=0)  # level size ~100M
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    s = tr.session
    exp, now = 5_000_000, 1
    for i in range(1, 22):  # ~3000 EXP/min for ~3.5 min
        exp, now = exp + 500, now + 10
        tr.on_status(45, exp, exp / 1e6, now=now)
        if i == 15:  # a single misread, then back to normal
            tr.on_status(45, exp + 6_000_000, exp / 1e6, now=now + 5)
    assert s.best_minute() < 10_000
    assert s.exp.gained == exp - 5_000_000
    assert s.exp.lost == 0
    rec = s.to_dict()
    assert rec["exp_gained"] == exp - 5_000_000 and rec["exp_lost"] == 0


def test_level_misread_does_not_poison_exp_table(tmp_path):
    # Review repro: one stray "31" at level 30 used to learn {30: 100000, 31: 100000}.
    tr = make(tmp_path)
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_status(30, 10_000, 10.0, now=0)
    for i, (lvl, exp) in enumerate([(30, 10_100), (30, 10_200), (31, 10_300), (30, 10_400), (30, 10_500)]):
        tr.on_status(lvl, exp, exp / 1000, now=1 + i)
    assert tr.session.exp.exp_table == {30: 100_000}
    assert tr.session.exp.levels_gained == 0
    tr.end_session()
    assert tr.exp_table == {30: 100_000}


def test_exp_table_needs_agreeing_estimates_and_can_be_corrected(tmp_path):
    tr = make(tmp_path)
    tr.exp_table[30] = 50_000  # wrong entry from an earlier misread
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_status(30, 10_000, 10.0, now=0)
    tr.on_status(30, 10_100, 10.1, now=1)
    assert tr.session.exp.exp_table[30] == 50_000  # one estimate isn't enough
    for i in range(2, 5):
        exp = 10_000 + 100 * i
        tr.on_status(30, exp, exp / 1000, now=i)
    assert tr.session.exp.exp_table[30] == 100_000


def test_session_end_flushes_pending_level_up(tmp_path):
    tr = make(tmp_path)
    tr.exp_table.update({45: 10_000, 46: 20_000})
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_status(45, 9_800, 98.0, now=0)
    tr.on_status(45, 9_900, 99.0, now=1)
    tr.on_status(46, 50, 0.25, now=2)  # level-up on the last reading
    tr.on_map("Town", [])
    rec = json.loads(tr.saved[-1].read_text(encoding="utf-8"))
    assert rec["exp_gained"] == 100 + 150 and rec["levels_gained"] == 1
