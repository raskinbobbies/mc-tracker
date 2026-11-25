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
    for i in range(20):
        gain += 24
        b.line(t, 22)  # a line and its bonus line, but not as a clean pair
        if i % 4 == 0:
            gain += 24  # a kill whose line was missed
        b.update(gain, t + 0.5, party=True)
        t += 1
    b.update(gain, t + b.lag_s + 1, party=True)
    # Each line read is a kill, but with no pair the epoch has no U: the 5
    # kills whose lines were missed don't count.
    assert b._pairs == 0 and b._U == 0
    assert b.total == 23


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


def _two_mobs(b, t=0.0, gain=0):
    """Read pairs of both mobs (A 20+2, B 24+2), then let the gain settle."""
    for v in (20, 24, 20, 24):
        t += 2
        gain += kill(b, t, v=v, read_bonus=2)
        b.update(gain, t, party=True)
    for _ in range(40):
        t += 0.5
        b.shown(t)
        b.update(gain, t, party=True)
    return t, gain


def _frames(b, t, gain, n=4):
    for _ in range(n):
        t += 0.5
        b.shown(t)
        b.update(gain, t, party=True)
    return t


def test_screen_pairs_of_the_smaller_mob_do_not_lock_u_low():
    # After a boundary three A kills repeat lines already on screen, then B's
    # kill line and bonus line are read but never in one frame as a pair.
    b = BarKills()
    t, gain = _two_mobs(b)
    real = 4
    b.line(t, 20)
    b.bonus(2)
    b.pair(22, 2)
    gain += 22
    real += 1
    b.update(gain, t, party=True)
    t = _frames(b, t, gain, 20)
    b.boundary()
    for _ in range(3):
        gain += 22
        real += 1
        t = _frames(b, t, gain)
    assert b._U >= 26  # B's 24 is on screen: the screen pair takes its pair
    t += 0.5
    b.line(t, 24)
    b.bonus(2)
    gain += 26
    real += 1
    t = _frames(b, t, gain)
    for _ in range(20):
        gain += 26
        real += 1
        t = _frames(b, t, gain)
    t = _frames(b, t, gain, 40)
    assert real == 29
    assert 26 <= b.total <= real


def test_a_paid_kill_and_bonus_line_raises_u_without_arming_the_larger_pair_rule():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, v=20, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    assert b._U == 22
    b.line(3, 24)  # B's kill line and bonus line, not read as a pair
    b.bonus(2)
    b.shown(3)
    gain += 26
    b.update(gain, 3.5, party=True)
    assert b.total == 4 and b._U == 26 and b._read == 3


def test_a_misread_with_no_u_yet_is_not_paid():
    # Right after a boundary "+24" is read as "+2" (its "+2" bonus read right):
    # with no U the guard can't charge U, so the line isn't paid at all.
    b = BarKills()
    t, gain = _two_mobs(b)
    real = 4
    b.boundary()
    for _ in range(2):
        t += 0.5
        gain += kill(b, t, v=2, bonus=24, read_bonus=2)
        real += 1
        t = _frames(b, t, gain)
    for _ in range(6):
        gain += 26
        real += 1
        t = _frames(b, t, gain)
    t = _frames(b, t, gain, 40)
    assert real == 12
    assert 10 <= b.total <= real


def test_an_epoch_with_two_mobs_built_from_screen_pairs():
    # Both mobs' lines are on screen; after a boundary only bar steps come.
    b = BarKills()
    t, gain = _two_mobs(b)
    for v in (20, 24):  # keep both values on screen
        t += 0.5
        kill(b, t, v=v)
    b.boundary()
    real = 4
    for i in range(24):
        gain += 22 if i % 2 else 26
        real += 1
        t = _frames(b, t, gain)
    t = _frames(b, t, gain, 40)
    assert b._U == 26 and b._read == 0
    assert 24 <= b.total <= real


def test_screen_values_clear_after_a_gap_with_no_exp_line():
    b = BarKills()
    b.line(0, 20)
    b.shown(1)
    assert 20 in b._screen
    b.shown(1 + b.screen_gap_s + 0.1)
    assert not b._screen
    assert b._screen_pair(22) == 0
