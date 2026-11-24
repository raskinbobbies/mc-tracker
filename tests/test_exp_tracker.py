from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.exp_tracker import (
    DEATH_CONFIRM,
    DISTRUST_MIN_READINGS,
    LEARN_MIN_READINGS,
    TRUST_BACK_READINGS,
    ExpTracker,
    Reading,
)


def rd(t, level, exp, digits=None, pct="auto"):
    """A bar reading at true ``level``; ``digits`` is what the level OCR saw."""
    if pct == "auto":
        pct = bar_percent(exp, EXP_TABLE[level])
    return Reading(t, level if digits is None else digits, exp, pct)


def at(level, frac):
    return round(EXP_TABLE[level] * frac)


def test_table_reproduces_level_45_points():
    assert EXP_TABLE[45] == 468_450
    assert bar_percent(74_473, EXP_TABLE[45]) == 15.90  # owner's bar: rounded, not truncated
    assert bar_percent(74_069, EXP_TABLE[45]) == 15.81  # reference tracker
    t = ExpTracker()
    assert t.fits(Reading(0, 45, 74_473, 15.90)) == [45]
    assert t.fits(Reading(0, 99, 74_473, 15.90)) == [45]  # the digits don't matter


def test_same_level_rate():
    t = ExpTracker()
    t.add(rd(0, 10, 100))
    t.add(rd(1800, 10, 600))
    assert t.gained == 500
    assert t.rate_per_hour() == 1000


def test_level_up_books_the_remainder_and_new_exp():
    t = ExpTracker()
    t.add(rd(0, 10, 1600))
    assert t.add(rd(60, 11, 50)) == 116 + 50
    assert t.levels_gained == 1


def test_level_30_to_31_then_map_change():
    # Review: level 30 at 40%, then level 31 at 11% for 10 readings.
    t = ExpTracker()
    start = at(30, 0.40)
    t.add(rd(0, 30, start))
    exp = at(31, 0.11)
    for i in range(10):
        t.add(rd(1 + i, 31, exp + i * 10))
    t.finalize()
    assert t.levels_gained == 1
    assert t.gained == EXP_TABLE[30] - start + exp + 90


def test_quest_double_level_up():
    t = ExpTracker()
    t.add(rd(0, 30, at(30, 0.9)))
    t.add(rd(1, 32, at(32, 0.2)))
    assert t.levels_gained == 2
    assert t.gained == EXP_TABLE[30] - at(30, 0.9) + EXP_TABLE[31] + at(32, 0.2)


def test_plus_one_level_misread_held_long_books_nothing():
    t = ExpTracker()
    exp = at(30, 0.40)
    t.add(rd(0, 30, exp))
    for i in range(35):
        t.add(rd(1 + i, 30, exp, digits=31))
    t.finalize()
    assert t.levels_gained == 0 and t.gained == 0 and t.last.level == 30


def test_level_misread_lower_while_exp_rises():
    t = ExpTracker()
    exp = at(30, 0.40)
    t.add(rd(0, 30, exp))
    for i in range(40):
        t.add(rd(1 + i, 30, exp + 50 * (i + 1), digits=3))
    t.finalize()
    assert t.gained == 50 * 40 and t.levels_gained == 0
    assert t.need(3) == EXP_TABLE[3] and 3 not in t.distrusted and 3 not in t.learned


def test_death_is_booked_once():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    t.add(rd(1, 30, 40_500))
    loss = at(30, 0.10)
    after = 40_500 - loss
    for i in range(6):
        t.add(rd(2 + i, 30, after + 10 * i))
    assert t.lost == loss
    assert t.gained == 500 + 50  # gains before and after the death
    assert t.net == 500 + 50 - loss


def test_short_dip_is_a_misread_not_a_death():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    t.add(rd(1, 30, 40_500))
    for _ in range(DEATH_CONFIRM - 1):
        assert t.add(rd(2, 30, 39_000)) == 0
    assert t.add(rd(3, 30, 40_600)) == 100
    assert t.lost == 0 and t.gained == 600


def test_drop_too_big_for_a_death_is_not_a_loss():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    t.add(rd(1, 30, 40_500))
    for i in range(5):
        t.add(rd(2 + i, 30, 10_000))  # -32% of the level: not a classic death
    assert t.lost == 0


def test_spike_is_corrected_by_the_next_good_reading():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    t.add(rd(1, 30, 40_100))
    assert t.add(rd(2, 30, 60_000)) == 19_900  # validated, but wrong
    assert t.add(rd(3, 30, 40_200)) == -19_800
    assert t.gained == 200 and t.lost == 0


def test_unreadable_percent_does_not_move():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    for i in range(20):
        assert t.add(rd(1 + i, 30 + i % 3, 50_000 + i * 999, pct=None)) == 0
    assert t.gained == 0 and t.last.exp == 40_000
    assert t.finalize() == 0  # the readings don't agree on a level


def test_unreadable_percent_same_level_gain_is_booked_at_session_end():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    for i in range(3):
        t.add(rd(1 + i, 30, 41_000 + i, pct=None))
    assert t.gained == 0
    assert t.finalize() == 1_000
    assert t.gained == 1_000


def test_unreadable_percent_past_the_level_size_is_not_booked():
    t = ExpTracker()
    t.add(rd(0, 30, 40_000))
    for i in range(3):
        t.add(rd(1 + i, 30, 400_000, pct=None))
    assert t.finalize() == 0


def test_level_up_at_0_percent_counted_at_session_end():
    t = ExpTracker()
    t.add(rd(0, 30, at(30, 0.95)))
    t.add(rd(1, 31, 3))  # 0.00%: fits many levels
    assert t.levels_gained == 0
    t.finalize()  # map change
    assert t.levels_gained == 1
    assert t.gained == EXP_TABLE[30] - at(30, 0.95) + 3


def test_zero_percent_after_level_up_then_resolves():
    t = ExpTracker()
    t.add(rd(0, 30, at(30, 0.99)))
    for i in range(5):
        assert t.add(rd(1 + i, 31, i)) == 0  # 0.00%: ambiguous, nothing moves
    t.add(rd(10, 31, at(31, 0.02)))
    assert t.levels_gained == 1
    assert t.gained == EXP_TABLE[30] - at(30, 0.99) + at(31, 0.02)


def test_low_level_big_kill_is_counted():
    t = ExpTracker()
    t.add(rd(0, 5, 20))
    t.add(rd(1, 5, 80))  # +60 of 135: one kill is 44% of the level
    t.add(rd(2, 6, 10))
    assert t.gained == 60 + (135 - 80) + 10 and t.levels_gained == 1


def test_ambiguous_first_reading_takes_lowest_gain_start():
    t = ExpTracker()
    t.add(rd(0, 31, 2))  # 0.00%
    t.add(rd(1, 31, at(31, 0.05)))
    assert t.gained == at(31, 0.05) - 2 and t.levels_gained == 0


def test_rolling_window_drops_old_samples():
    t = ExpTracker(window_s=60)
    t.add(rd(0, 10, 0))
    t.add(rd(30, 10, 0))  # idle
    t.add(rd(100, 10, 0))
    t.add(rd(130, 10, 100))
    assert t.rate_per_hour() == 100 / 30 * 3600


def test_time_to_level_uses_table():
    t = ExpTracker()
    t.add(rd(0, 10, 716))
    t.add(rd(3600, 10, 1216))
    assert t.exp_remaining() == 500
    assert t.seconds_to_level() == 3600


def test_eta_prefers_session_average_over_rolling_rate():
    t = ExpTracker(window_s=60)
    t.add(rd(0, 10, 0))
    t.add(rd(3000, 10, 0))
    t.add(rd(3600, 10, 600))
    assert t.seconds_to_level() == (1716 - 600) / 600 * 3600


def test_eta_none_without_rate_or_remaining():
    t = ExpTracker()
    t.add(rd(0, 10, 100))
    t.add(rd(10, 10, 100))
    assert t.seconds_to_level() is None
    assert ExpTracker().exp_remaining() is None


def learn_readings(t, level, need, n=LEARN_MIN_READINGS + 2, start_frac=0.4):
    for i in range(n):
        exp = round(need * start_frac) + 137 * i
        t.add(Reading(i, level, exp, bar_percent(exp, need)))


def committed_at(level, frac=0.5):
    t = ExpTracker()
    t.add(rd(-1, level, at(level, frac)))
    return t


def test_learns_a_level_missing_from_the_table():
    need = 11_938_059
    t = committed_at(89)
    learn_readings(t, 90, need, n=30)
    assert abs(t.learned[90] - need) / need < 0.002
    assert t.level == 90 and t.levels_gained == 1  # crossed once the size was known
    assert t.exp_remaining() is not None


def test_no_learning_without_a_committed_neighbour():
    t = ExpTracker()
    learn_readings(t, 90, 11_938_059, n=30)
    assert t.learned == {}
    t = committed_at(45)
    learn_readings(t, 90, 11_938_059, n=30)  # not adjacent to level 45
    assert t.learned == {}


def test_no_level_crossing_from_a_learned_size_alone():
    t = ExpTracker(learned={90: 11_938_059})
    t.add(Reading(0, 89, 5_000_000, bar_percent(5_000_000, EXP_TABLE[89])))
    # Fits learned level 90, but the digits say 89: not booked.
    t.add(Reading(1, 89, 100_000, bar_percent(100_000, 11_938_059)))
    assert t.levels_gained == 0 and t.gained == 0


def test_table_mismatch_is_flagged_and_entry_distrusted():
    real = 100_000  # the server's level 30 differs from the table's 95,700
    t = committed_at(29, 0.99)
    learn_readings(t, 30, real, n=LEARN_MIN_READINGS)
    assert 30 not in t.distrusted  # overruling the table needs much more
    learn_readings(t, 30, real, n=DISTRUST_MIN_READINGS + 5)
    assert 30 in t.distrusted and t.warnings
    assert t.need(30) is not None and abs(t.need(30) - real) / real < 0.002


def test_distrusted_entry_is_restored_when_the_table_fits_again():
    t = ExpTracker(learned={30: 100_000}, distrusted={30})
    t.add(rd(0, 30, 40_000))
    for i in range(TRUST_BACK_READINGS):
        t.add(rd(1 + i, 30, 40_000 + 100 * i))
    assert 30 not in t.distrusted and 30 not in t.learned
    assert t.need(30) == EXP_TABLE[30]
