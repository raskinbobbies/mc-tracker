"""Mesos booked once per line on screen (``MesosLines``), whatever LineDiff makes of the area."""
from mctracker.mesos import MesosLines, parse_mesos

M = "You have gained mesos (+{})".format
K = "You received EXP (+13)"
B = "Bonus EXP for PARTY (+4)"
P = 21.0  # row pitch


def area(*texts):
    """Rows bottom-aligned, oldest first, as (text, y)."""
    n = len(texts)
    return [(t, 272.0 - P * (n - 1 - i)) for i, t in enumerate(texts) if t is not None]


def run(frames, dt=0.15):
    m, total = MesosLines(), 0
    for i, f in enumerate(frames):
        total += m.update(f, i * dt)
    return total


def test_strict_parse_needs_the_closing_bracket():
    assert parse_mesos("You have gained mesos (+76)") == 76
    assert parse_mesos("You have gained mesos (+1,234))") == 1234
    assert parse_mesos("You have gained mesos (+7o))") is None  # "+76" misread: no amount
    assert parse_mesos("You have gained mesos (+84") is None


def test_a_line_read_again_after_hidden_frames_counts_once():
    up = area(K, B, M(80))
    assert run([up] * 4 + [area(K, B, None)] * 3 + [up] * 4) == 80


def test_two_identical_pickups_in_a_row_count_twice():
    first = area(K, B, M(80))
    second = area(K, B, M(80), K, B, M(80))
    assert run([first] * 4 + [second] * 4) == 160


def test_the_second_identical_pickup_counts_even_if_the_first_was_unread_as_it_came_in():
    first = area(K, B, M(80))
    hidden = area(K, B, None, K, B, M(80))  # the older line not read this frame
    both = area(K, B, M(80), K, B, M(80))
    assert run([first] * 4 + [hidden, both, both]) == 160


def test_a_one_off_misread_is_never_booked():
    up = area(K, B, M(88))
    assert run([up] * 3 + [area(K, B, M(83))] + [up] * 3) == 88


def test_a_line_scrolling_up_counts_once():
    frames = [area(K, B, M(75))] * 3 + [area(K, B, M(75), K, B)] * 3 + [area(K, B, M(75), K, B, K, B)] * 3
    assert run(frames) == 75


def test_a_scroll_hidden_by_a_repeating_pattern_still_shows_the_new_pickup():
    # The same kill and pickup again: the area looks the same at a glance, but rows changed in place.
    a = area(K, B, M(80), "You received EXP (+28)", "Bonus EXP for PARTY (+9)", M(80))
    b = area("You received EXP (+28)", "Bonus EXP for PARTY (+9)", M(80), K, B, M(80))
    assert run([a] * 3 + [b] * 3) == 240


def test_a_bracket_read_as_a_digit_is_dropped_against_recent_pickups():
    frames = []
    for v in (70, 75, 80, 72, 78):  # each pickup up for a while, then faded
        frames += [area(K, B, M(v))] * 3 + [[]] * 4
    frames += [area(K, B, "You have gained mesos (+801)")] * 3
    assert run(frames, dt=2.0) == 70 + 75 + 80 + 72 + 78 + 80


def test_a_new_pickup_after_the_area_faded_counts():
    up = area(K, B, M(80))
    assert run([up] * 3 + [[]] * 4 + [up] * 3, dt=2.0) == 160
