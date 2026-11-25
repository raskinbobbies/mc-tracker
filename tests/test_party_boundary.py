"""Party kills after an epoch boundary, and lines read low (#22)."""
from mctracker.kills import BarKills


def kill(b, t, v=22, bonus=2, read=True, read_bonus=None):
    """One party kill at ``t`` as the session feeds it: the kill line, its
    bonus line and the pair (when both were read), with EXP lines on screen."""
    if read:
        b.line(t, v)
        if read_bonus is not None:
            b.bonus(read_bonus)
            b.pair(v + read_bonus, read_bonus)
    b.shown(t)
    return v + bonus


def test_count_keeps_moving_after_a_boundary_with_identical_lines_on_screen():
    # One monster, a steady share: after a level-up the message area stays
    # full of the same lines, so nothing new is read and no new pair comes.
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    assert b.total == 3
    b.boundary()  # a level-up (or a widget row change)
    t = 3.0
    for _ in range(20):
        gain += kill(b, t, read=False)  # the same text again: not new
        for f in range(4):  # the area keeps showing EXP lines
            b.shown(t + f * 0.25)
            b.update(gain, t + f * 0.25, party=True)
        t += 1
    for f in range(int(b.lag_s * 4) + 4):
        b.shown(t + f * 0.25)
        b.update(gain, t + f * 0.25, party=True)
    assert b.total == 23


def test_without_lines_on_screen_a_new_epoch_still_waits_for_pairs():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    b.boundary()
    t = 3 + b.screen_gap_s + 1  # the area emptied: what it showed is gone
    for _ in range(20):
        gain += 24
        b.line(t, 22)  # a line and its bonus line, but not as a clean pair
        b.update(gain, t + 0.5, party=True)
        t += 1
    b.update(gain, t + b.lag_s + 1, party=True)
    assert b.total <= 23


def test_a_kill_line_read_low_with_its_bonus_is_not_counted_twice():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    for i in range(3, 9):  # "+22" read as "+2", its "+2" bonus read right
        gain += kill(b, i, v=2, bonus=22, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    b.update(gain, 9 + b.lag_s + 1, party=True)
    assert b.total <= 9
    assert max(b.per_kill) == 24 and 4 not in b.per_kill  # the misread pair isn't learnt


def test_a_kill_line_read_low_without_its_bonus_costs_a_whole_kill():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    t = 3
    for _ in range(6):  # "+22" read as "+2", its bonus line missed (too far apart to be re-reads)
        gain += kill(b, t, v=2, bonus=22)
        b.update(gain, t + 0.5, party=True)
        t += b.repeat_s + 1
    b.update(gain, t + b.lag_s + 1, party=True)
    assert b.total == 9


def test_a_bonus_line_read_low_costs_the_bonus_seen_with_that_kill():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, v=120, bonus=12, read_bonus=12)
        b.update(gain, i + 0.5, party=True)
    for i in range(3, 27):  # "+12" read as "+1"
        gain += kill(b, i, v=120, bonus=12, read_bonus=1)
        b.update(gain, i + 0.5, party=True)
    b.update(gain, 27 + b.lag_s + 1, party=True)
    assert b.total <= 27
