import json

import pytest

from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.kills import Mob
from mctracker.session import Tracker

HUNT = [Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98), Mob(58, "Echopus", 107)]
EXP = "You received EXP (+{})"


def status(tr, exp, now, level=45, pct="auto"):
    """A bar reading whose percent matches the EXP table."""
    tr.on_status(level, exp, bar_percent(exp, EXP_TABLE[level]) if pct == "auto" else pct, now=now)


def ticks(target, start, end):
    """The UI refresh: a tick every second (a gap over SUSPEND_GAP_S is the PC asleep, not session time)."""
    for t in range(start, end + 1):
        target.tick(now=t)


def make(tmp_path):
    return Tracker(save_dir=tmp_path, item_resolver={"Echopus Leg": 4000055}.get)


def test_town_never_starts_a_session(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Forgotten Hollow", [])
    status(tr, 100_000 + 100, now=0)
    status(tr, 100_000 + 500, now=1)  # quest reward in town
    tr.on_message_lines([(EXP.format(400), 0)], now=1)
    assert tr.state == "waiting" and tr.session is None


def test_first_exp_starts_session_on_hunting_map(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    assert tr.session is None
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    assert tr.state == "active"
    status(tr, 100_000 + 1082, now=2)
    assert tr.session.exp.gained == 82
    assert tr.session.kills == {54: 1}


def test_leaving_map_saves_session_and_waits(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    status(tr, 100_000 + 1082, now=2)
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
    status(tr, 100_000 + 1000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    status(tr, 100_000 + 1082, now=2)
    tr.restart()
    assert make(tmp_path).today()["exp"] == 82  # reloaded from disk


def test_session_time_is_the_wall_clock_idle_included(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    status(tr, 100_000 + 1100, now=10)  # gain -> session starts
    status(tr, 100_000 + 1200, now=20)
    s = tr.session
    assert s.active_s == 10
    ticks(s, 21, 500)  # AFK for 8 minutes
    assert s.is_idle(now=500)
    assert s.active_s == 490  # idle time counts
    assert s.grind_s <= 70  # the tracker's own clock still skips it (counts up to the idle mark)
    status(tr, 100_000 + 1300, now=510)
    assert s.active_s == 500


@pytest.fixture
def clock(monkeypatch):
    """A hand-set ``time.monotonic`` for the calls that read it themselves (pause, resume, ticks)."""
    from mctracker import session as session_mod

    now = [0.0]
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: now[0])
    return now


def running(tmp_path, clock):
    """A tracker whose session started at clock 10 (baseline at 0, first gain at 10)."""
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    clock[0] = 10
    status(tr, 100_000 + 1100, now=10)
    return tr


def test_pause_and_resume_stop_the_wall_clock(tmp_path, clock):
    tr = running(tmp_path, clock)
    s = tr.session
    clock[0] = 30
    tr.pause()  # ticks to 30, then stops
    assert s.paused and s.active_s == 20
    clock[0] = 100
    s.tick()  # a refresh while paused adds nothing
    assert s.active_s == 20
    tr.resume()  # the 70 s paused never count
    clock[0] = 110
    s.tick()
    assert s.active_s == 30 and not s.paused


def test_saved_seconds_are_wall_time_minus_pauses(tmp_path, clock):
    tr = running(tmp_path, clock)
    ticks(tr, 11, 100)
    clock[0] = 100
    tr.pause()
    clock[0] = 160
    tr.resume()
    status(tr, 100_000 + 1200, now=170)
    clock[0] = 200
    tr.tick()  # the refresh before the session ends
    s = tr.session
    path = tr.end_session()  # no closing tick of its own
    assert json.loads(path.read_text(encoding="utf-8"))["active_seconds"] == 130  # 200 - 10 - 60 paused
    assert s.active_s == 130


def test_rate_and_avg_use_wall_time_and_current_rate_reads_zero_when_idle(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    status(tr, 100_000 + 6000, now=2)
    status(tr, 100_000 + 9000, now=5)
    s = tr.session
    assert s.rate_per_hour(now=5) > 0  # still gaining
    ticks(s, 6, 902)  # 15 minutes without a gain
    assert s.is_idle(now=902) and s.rate_per_hour(now=902) == 0.0
    assert s.session_rate_per_hour() == pytest.approx(s.exp.gained / s.active_s * 3600)
    assert s.active_s > 890  # AVG divides by the wall time, not the grinding time


def test_idle_minutes_show_as_zero(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    status(tr, 100_000 + 1100, now=2)
    ticks(tr, 3, 999)
    status(tr, 100_000 + 1200, now=1000)  # long idle gap, then gain
    mins = tr.session.exp_per_minute
    assert len(mins) == 17 and mins[0] > 0 and mins[1:-1] == [0] * 15 and mins[-1] > 0


def test_pause_ignores_gains(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    status(tr, 100_000 + 1082, now=2)
    tr.pause()
    tr.on_message_lines([(EXP.format(82), 0), (EXP.format(98), 22)], now=3)
    status(tr, 100_000 + 1500, now=4)
    tr.resume()
    status(tr, 100_000 + 1500, now=5)  # new baseline
    status(tr, 100_000 + 1600, now=6)
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
    assert s.kills_read == 2 and s.total_kills == 0  # kills wait for the EXP bar
    assert s.message_exp == 33 + 3 + 64 + 6
    assert s.loot == {4000055: 1}


def test_exp_per_minute_uses_active_clock(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000 + 1000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    s = tr.session
    for i, now in enumerate(range(2, 130, 2)):  # 100 EXP every 2 s for ~2 min
        status(tr, 100_000 + 1082 + 100 * i, now=now)
    mins = s.exp_per_minute
    assert len(mins) == 3 and sum(mins) == s.exp.gained
    assert s.best_minute() == max(mins) > 0
    assert s.to_dict()["exp_per_minute"] == mins


def test_one_off_exp_spike_does_not_inflate_best_minute_or_totals(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    status(tr, 100_000, now=0)
    tr.on_message_lines([(EXP.format(82), 0)], now=1)
    s = tr.session
    exp, now = 100_000, 1
    for i in range(1, 22):  # ~3000 EXP/min for ~3.5 min
        exp, now = exp + 500, now + 10
        status(tr, exp, now=now)
        if i == 15:  # one misread that still fits the bar, then back to normal
            status(tr, exp + 200_000, now=now + 5)
    assert s.best_minute() < 10_000
    assert s.exp.gained == exp - 100_000
    assert sum(s.exp_per_minute) == s.exp.gained
    rec = s.to_dict()
    assert rec["exp_gained"] == exp - 100_000 and rec["exp_lost"] == 0


def test_level_misread_does_not_book_or_learn(tmp_path):
    # Review repro: a stray "31" at level 30.
    tr = make(tmp_path)
    tr.on_map("Ant Tunnel", HUNT)
    status(tr, 10_000, now=0, level=30)
    for i, (lvl, exp) in enumerate([(30, 10_100), (30, 10_200), (31, 10_300), (30, 10_400), (30, 10_500)]):
        tr.on_status(lvl, exp, bar_percent(exp, EXP_TABLE[30]), now=1 + i)
    assert tr.session.exp.levels_gained == 0 and tr.session.exp.gained == 500
    tr.end_session()
    assert tr.learned_exp == {} and tr.distrusted == set()


def test_level_up_at_map_change_is_counted(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Ant Tunnel", HUNT)
    status(tr, 467_000, now=0)
    status(tr, 467_500, now=1)
    status(tr, 4, now=2, level=46)  # 0.00% on the last reading: fits many levels
    tr.on_map("Town", [])
    rec = json.loads(tr.saved[-1].read_text(encoding="utf-8"))
    assert rec["levels_gained"] == 1
    assert rec["exp_gained"] == 500 + (468_450 - 467_500) + 4


def test_correction_is_taken_back_from_the_minute_that_booked_it(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Ant Tunnel", HUNT)
    status(tr, 100_000, now=0)
    status(tr, 100_100, now=1)
    tr.tick(now=25)
    status(tr, 140_000, now=50)  # bad reading in minute 0 (small enough to commit)
    for i in range(1, 6):
        status(tr, 100_100 + 100 * i, now=60 + 10 * i)  # minute 1
    mins = tr.session.exp_per_minute
    assert mins[0] == 100 and sum(mins) == 600 and min(mins) >= 0


def test_learned_sizes_carry_to_the_next_session(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Ant Tunnel", HUNT)
    need = 11_938_059  # level 90 isn't in the shipped table
    status(tr, 5_000_000, now=0, level=89)
    status(tr, 5_000_100, now=1, level=89)
    for i in range(30):
        exp = 5_000_000 + 137 * i
        tr.on_status(90, exp, bar_percent(exp, need), now=2 + i)
    tr.end_session()
    assert abs(tr.learned_exp[90] - need) / need < 0.002


def test_exp_earned_while_paused_without_a_session_is_not_booked(tmp_path):
    # Review of a989035: paused while waiting, the probe kept its pre-pause
    # reading and booked everything earned meanwhile into the next session.
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    base = 100_000
    status(tr, base, now=0, level=50)
    tr.pause()
    status(tr, base + 70_920, now=10, level=50)  # earned while paused (under the step cap)
    tr.resume()
    status(tr, base + 70_920, now=11, level=50)
    assert tr.session is None  # the stale baseline is gone: nothing to book
    status(tr, base + 71_020, now=12, level=50)
    assert tr.session is not None and tr.session.exp.net == 100


def test_exp_earned_while_paused_then_new_session_is_not_booked(tmp_path):
    tr = make(tmp_path)
    tr.on_map("Primeval Forest I", HUNT)
    base = 100_000
    status(tr, base, now=0, level=50)
    status(tr, base + 300, now=1, level=50)
    assert tr.session is not None
    tr.pause()
    status(tr, base + 300 + 35_460, now=10, level=50)  # earned while paused
    tr.restart()  # "New session"
    status(tr, base + 300 + 35_460, now=11, level=50)
    assert tr.session is None
    status(tr, base + 300 + 36_060, now=12, level=50)
    assert tr.session is not None and tr.session.exp.net == 600


def test_current_rate_falls_smoothly_to_zero_after_the_last_gain(tmp_path, clock):
    tr = running(tmp_path, clock)
    status(tr, 100_000 + 7100, now=14)  # the last gain, 4 s into the session
    s = tr.session
    rates = [s.rate_per_hour(now=n) for n in (14, 44, 69, 73)]
    assert rates[0] > rates[1] > rates[2] > rates[3] > 0  # smoothly down, not held then dropped
    assert s.rate_per_hour(now=74) == 0.0  # its minute is over
    assert s.rate_per_hour(now=500) == 0.0


def died(tmp_path, clock):
    """Kills to +5,800 EXP, a death that costs 10% of level 45, then more kills; 3 minutes in."""
    tr = running(tmp_path, clock)
    need = EXP_TABLE[45]
    for i, e in enumerate(range(1200, 7200, 300)):
        status(tr, 100_000 + e, now=11 + i)
    top = 100_000 + 6900
    for i in range(6):  # the death: a 10% drop, held (a dip needs later frames to be confirmed)
        status(tr, top - need // 10, now=40 + i)
    for i, e in enumerate(range(300, 3300, 300)):  # kills after respawning
        status(tr, top - need // 10 + e, now=60 + i)
    assert tr.session.rate_per_hour(now=70) > 0  # the kills after the death, the loss not netted in
    ticks(tr.session, 11, 190)
    return tr


def test_a_death_never_makes_the_rates_negative(tmp_path, clock):
    tr = died(tmp_path, clock)
    s = tr.session
    assert s.exp.deaths == 1 and s.exp.lost > 0
    assert s.exp.net < 0  # the loss is bigger than what was gained: the old net rate went negative
    assert s.session_rate_per_hour() == pytest.approx(s.exp.gained / s.active_s * 3600)
    assert s.session_rate_per_hour() > 0
    assert all(m >= 0 for m in s.exp_per_minute) and sum(s.exp_per_minute) == s.exp.gained
    eta = s.seconds_to_level()  # the gained rate against what's left from the current EXP
    assert eta == pytest.approx(s.exp.exp_remaining() / s.session_rate_per_hour() * 3600)
    assert s.exp.exp_remaining() == EXP_TABLE[45] - s.exp.last.exp


def test_a_death_is_saved_apart_from_the_rate(tmp_path, clock):
    tr = died(tmp_path, clock)
    s = tr.session
    d = s.to_dict()
    assert d["deaths"] == 1 and d["exp_lost"] == s.exp.lost
    assert d["exp_per_hour"] == round(d["exp_gained"] / s.active_s * 3600) > 0


def test_eta_uses_the_wall_clock_average(tmp_path, clock):
    tr = running(tmp_path, clock)
    ticks(tr, 11, 69)
    status(tr, 100_000 + 7100, now=70)
    s = tr.session
    s.tick(now=70)
    left = s.exp.exp_remaining()
    first = s.seconds_to_level()
    assert first == pytest.approx(left / (s.exp.gained / s.active_s))
    ticks(s, 71, 670)  # ten idle minutes: the same EXP over more time, so the ETA grows
    assert s.is_idle(now=670) and s.seconds_to_level() > 5 * first
