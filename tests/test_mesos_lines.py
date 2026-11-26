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


def test_the_same_misread_twice_is_a_misread_of_the_line_not_a_pickup():
    # One 80 line, read as 88 in two frames 5 s apart (once before and once after it scrolled).
    frames = ([area(K, M(80))] * 3 + [area(K, M(88))] + [area(K, M(80))] * 5
              + [area(K, M(80), K, B)] * 5 + [area(K, M(88), K, B)] + [area(K, M(80), K, B)] * 3)
    assert run(frames) == 80


def test_a_new_line_where_an_old_one_sat_after_a_scroll_nothing_lined_up_for_counts():
    # The area scrolled two rows and every old row is misread or unread: the scroll is unknown,
    # so the new 78 at the bottom is not taken as a misread of the 73 that sat there.
    before = area(B, "Ruby x1 earned. (Etc)", M(73))
    after = area(None, None, "You received EXP (+4)", "Bonus EXP for PARTY (+1)", M(78))
    assert run([before] * 3 + [after] * 3) == 73 + 78


def test_a_faded_line_does_not_swallow_the_next_identical_pickup():
    noise = [("~~", 100.0)]  # something unreadable still on screen: the area is not empty
    frames = [area(K, M(80)) + noise] * 10 + [noise] * 10 + [area(M(80)) + noise] * 5
    assert run(frames) == 160


def test_a_big_real_pickup_is_not_cut_to_a_tenth():
    frames = []
    for v in (80, 80, 80, 80, 80):
        frames += [area(K, M(v))] * 3 + [[]] * 4
    frames += [area(K, M(800))] * 3  # ends in 0: never a ")" read as a digit
    assert run(frames, dt=2.0) == 5 * 80 + 800


def test_a_line_read_a_little_low_is_the_same_line():
    class Line:  # OCR boxes carry a height: half of it is the tolerance
        def __init__(self, text, y, height=12.0):
            self.text, self.y, self.height = text, y, height
    up = [Line(t, y) for t, y in area(K, B, M(80))]
    low = up[:2] + [Line(up[2].text, up[2].y + 7)]
    assert run([up] * 3 + [low] * 2 + [up] * 3) == 80


def test_unread_rows_in_a_repeating_pattern_do_not_make_it_line_up_at_the_wrong_shift():
    rows = ([B, M(80), K] * 3)[:8]  # B M K B M K B M: three 80s, nothing scrolls

    def without(*gone):
        return area(*[None if i in gone else r for i, r in enumerate(rows)])
    assert run([without()] * 3 + [without(0, 1, 6), without(4, 7)] + [without()] * 3) == 3 * 80
