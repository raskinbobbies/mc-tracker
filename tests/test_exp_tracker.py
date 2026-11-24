from mctracker.exp_tracker import ExpTracker, Reading

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
    assert t.add(Reading(60, 11, 50)) == 150
    assert t.levels_gained == 1


def test_multi_level_up():
    t = ExpTracker(TABLE)
    t.add(Reading(0, 10, 900))
    assert t.add(Reading(60, 12, 10)) == 100 + 1200 + 10


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


def test_unknown_level_in_table_is_ignored():
    t = ExpTracker({})
    t.add(Reading(0, 10, 900))
    assert t.add(Reading(10, 11, 50)) == 0


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
