from mctracker.exp_tracker import LEVEL_HOLD_READINGS, ExpTracker, Reading

TABLE = {10: 1000, 11: 1200, 12: 1500}


def test_same_level_rate():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 100))
    t.add(Reading(1800, 10, 600))
    assert t.gained == 500
    assert t.rate_per_hour() == 1000


def test_level_up():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 900))
    assert t.add(Reading(60, 11, 50)) == 0  # a level change waits for confirmation
    assert t.add(Reading(61, 11, 60)) == 160
    assert t.levels_gained == 1


def test_multi_level_jump_is_a_misread():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 900))
    t.add(Reading(60, 12, 10))
    assert t.add(Reading(61, 12, 10)) == 0
    assert t.levels_gained == 0
    assert t.add(Reading(62, 10, 950)) == 50


def test_multi_level_jump_held_long_rebaselines_without_booking():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 900))
    for i in range(LEVEL_HOLD_READINGS):
        t.add(Reading(1 + i, 12, 10 + i))
    assert t.last.level == 12 and t.net == 0 and t.levels_gained == 0
    assert t.add(Reading(100, 12, 100)) == 100 - (10 + LEVEL_HOLD_READINGS - 1)


def test_death_counts_as_loss():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 500))
    t.add(Reading(10, 10, 450))
    assert t.lost == 50
    assert t.net == -50


def test_level_down_misread_is_ignored():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 11, 500))
    assert t.add(Reading(10, 1, 500)) == 0
    assert t.net == 0


def test_level_up_of_unknown_size_books_level_and_estimated_exp():
    t = ExpTracker({})
    t.add(Reading(0, 10, 900, pct=90.0))  # level 10 is ~1000 by the bar
    t.add(Reading(10, 11, 50, pct=0.4))
    assert t.add(Reading(11, 11, 60, pct=0.5)) == 100 + 60
    assert t.levels_gained == 1 and t.gained == 160


def test_level_up_of_unknown_size_without_percent_books_new_level_exp():
    t = ExpTracker({})
    t.add(Reading(0, 10, 900))
    t.add(Reading(10, 11, 50))
    assert t.add(Reading(11, 11, 60)) == 60
    assert t.levels_gained == 1


def test_level_up_from_a_low_bar_is_a_misread():
    t = ExpTracker({})
    t.add(Reading(0, 10, 300, pct=30.0))
    t.add(Reading(10, 11, 310, pct=31.0))
    t.add(Reading(11, 11, 320, pct=32.0))
    assert t.levels_gained == 0 and t.net == 0


def test_rolling_window_drops_old_samples():
    t = ExpTracker(TABLE, window_s=60)
    t.add(Reading(0, 10, 0))
    t.add(Reading(30, 10, 0))  # idle
    t.add(Reading(100, 10, 0))
    t.add(Reading(130, 10, 100))
    # window keeps samples from t>=70 → (100, 0) .. (130, 100)
    assert t.rate_per_hour() == 100 / 30 * 3600


def test_time_to_level():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 0))
    t.add(Reading(3600, 10, 500))
    assert t.seconds_to_level() == 3600


def test_eta_prefers_session_average_over_rolling_rate():
    t = ExpTracker(TABLE, window_s=60)
    t.add(Reading(0, 10, 0))
    t.add(Reading(3000, 10, 0))  # long stall, then a burst
    t.add(Reading(3600, 10, 600))
    # session average: 600 EXP in 3600 s; 400 left -> 2400 s
    assert t.seconds_to_level() == 2400


def test_eta_falls_back_to_rolling_rate():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 0))
    t.add(Reading(10, 10, 0))
    assert t.seconds_to_level() is None  # no rate at all


def test_eta_none_when_level_size_unknown():
    t = ExpTracker({10: 1000})
    t.add(Reading(0, 10, 900))
    t.add(Reading(60, 10, 990))
    t.add(Reading(120, 11, 5))  # level 11's size not learned yet
    t.add(Reading(121, 11, 6))
    assert t.exp_remaining() is None
    assert t.seconds_to_level() is None


def test_eta_none_when_nothing_remaining():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 900))
    t.add(Reading(60, 10, 1000))  # exp == need: nothing left, not "0m 00s"
    assert t.exp_remaining() is None
    assert t.seconds_to_level() is None


def test_exp_remaining():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 250))
    assert t.exp_remaining() == 750


def test_fmt_eta():
    from mctracker.overlay import fmt_eta

    assert fmt_eta(None) == "—"
    assert fmt_eta(30) == "<1m"
    assert fmt_eta(37 * 60 + 5) == "37m"
    assert fmt_eta(2 * 3600 + 14 * 60) == "2h 14m"
    assert fmt_eta(3600) == "1h 00m"


def grind(t: ExpTracker, level=45, start=1000, minutes=3, per_min=3000):
    """Steady ~3000 EXP/min at 1 reading per 10s; returns the last exp."""
    exp = start
    t.add(Reading(0, level, exp))
    for i in range(1, minutes * 6 + 1):
        exp += per_min // 6
        t.add(Reading(i * 10, level, exp))
    return exp


def test_misread_level_up_is_not_booked_and_eta_keeps_moving():
    t = ExpTracker({45: 1_000_000, 46: 1_100_000})
    exp = grind(t)
    now = 3 * 60
    before = t.gained
    t.add(Reading(now + 10, 46, 500))  # one stray "46" while really on 45
    assert t.gained == before and t.levels_gained == 0
    assert t.add(Reading(now + 20, 45, exp + 500)) == 500  # back on the real level
    assert t.levels_gained == 0 and t.rejected == 1
    exp += 500
    t.add(Reading(now + 30, 45, exp + 500))
    assert t.exp_remaining() == 1_000_000 - exp - 500
    assert t.seconds_to_level() is not None


def test_level_change_confirmed_by_two_readings():
    t = ExpTracker({45: 10_000, 46: 20_000})
    t.add(Reading(0, 45, 9_900))
    t.add(Reading(10, 46, 50))
    assert t.levels_gained == 0
    assert t.add(Reading(20, 46, 80)) == 100 + 80
    assert t.levels_gained == 1 and t.gained == 180


def test_two_level_misreads_do_not_book_a_fake_level_up():
    # Review repro: "30" read as "3" twice used to add a whole fake level.
    from mctracker.demo import EXP_TABLE

    t = ExpTracker(EXP_TABLE)
    for i, (lvl, exp) in enumerate([(30, 5000), (30, 5100), (3, 5100), (3, 5150), (30, 5200), (30, 5250)]):
        t.add(Reading(i, lvl, exp))
    assert t.gained == 250 and t.lost == 0
    assert t.levels_gained == 0


def test_one_lower_level_misread_neighbour():
    t = ExpTracker({30: 100_000, 31: 110_000})
    for i, (lvl, exp) in enumerate([(31, 500), (31, 600), (30, 600), (30, 650), (31, 700), (31, 750)]):
        t.add(Reading(i, lvl, exp))
    assert t.gained == 250 and t.levels_gained == 0


def test_long_held_level_drop_rebaselines_and_return_books_nothing_extra():
    t = ExpTracker({45: 10_000, 46: 20_000})
    t.add(Reading(0, 46, 500))  # suppose the baseline itself was right
    for i in range(LEVEL_HOLD_READINGS):
        t.add(Reading(1 + i, 45, 500))
    assert t.last.level == 45 and t.net == 0  # adopted, so it can't stay stuck
    t.add(Reading(40, 45, 600))
    assert t.gained == 100
    t.add(Reading(41, 46, 620))
    t.add(Reading(42, 46, 650))  # back on the real level: same-level change
    assert t.gained == 150 and t.levels_gained == 0
    assert t.add(Reading(43, 46, 700)) == 50


def test_flush_books_a_pending_level_up():
    t = ExpTracker({45: 10_000, 46: 20_000})
    t.add(Reading(0, 45, 9_900))
    t.add(Reading(10, 46, 50))  # last reading before the session ends
    assert t.flush() == 150
    assert t.levels_gained == 1 and t.flush() == 0


def test_flush_drops_a_pending_implausible_spike():
    t = ExpTracker({45: 100_000_000})
    exp = grind(t)
    t.add(Reading(190, 45, exp + 60_000_000))
    assert t.flush() == 0 and t.rejected == 1


def test_reset_baseline_flushes_pending():
    t = ExpTracker({45: 10_000, 46: 20_000})
    t.add(Reading(0, 45, 9_900))
    t.add(Reading(10, 46, 50))
    assert t.reset_baseline() == 150
    assert t.last is None


def test_spike_then_reversal_is_not_booked():
    t = ExpTracker({45: 100_000_000})
    exp = grind(t)
    gained = t.gained
    assert t.add(Reading(190, 45, exp + 6_000_000)) == 0  # OCR misread
    assert t.add(Reading(200, 45, exp + 500)) == 500
    assert t.gained == gained + 500 and t.lost == 0


def test_real_big_jump_is_booked_once_confirmed():
    t = ExpTracker({45: 100_000_000})
    exp = grind(t)
    t.add(Reading(190, 45, exp + 6_000_000))
    assert t.add(Reading(200, 45, exp + 6_000_100)) == 6_000_100
    assert t.lost == 0


def test_dip_misread_is_not_booked_as_loss():
    t = ExpTracker({45: 100_000_000})
    exp = grind(t, start=5_000_000)
    t.add(Reading(190, 45, 10))  # misread to a tiny number
    t.add(Reading(200, 45, exp + 500))
    assert t.lost == 0
