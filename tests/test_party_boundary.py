"""Party kills across boundaries, and lines read low (#22).

Party kills are counted from events (``BarKills``: trusted lines + bar steps
- matched pairs), so a misread value can't add kills and a boundary needs no
new pairs. These keep the scenarios the per-epoch formula was tested on."""
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


def settle(b, t, gain, secs=10.0):
    """The bar keeps being read with EXP lines on screen: pending events settle."""
    for f in range(int(secs * 4)):
        b.shown(t + f * 0.25)
        b.update(gain, t + f * 0.25, party=True)
    return t + secs


def test_count_keeps_moving_after_a_boundary_with_identical_lines_on_screen():
    # One monster, a steady share: after a level-up the message area stays
    # full of the same lines, so nothing new is read. Each bar step is a kill.
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    b.boundary()  # a level-up (or a widget row change)
    t = 4.0
    for _ in range(20):
        gain += kill(b, t, read=False)  # the same text again: not new
        for f in range(4):  # the area keeps showing EXP lines
            b.shown(t + f * 0.25)
            b.update(gain, t + f * 0.25, party=True)
        t += 1
    settle(b, t, gain)
    assert b.total == 23


def test_lines_and_missed_kills_after_a_boundary():
    # Kill and bonus lines read, and a kill whose line was missed every
    # fourth step (in the same bar step as a read one).
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    b.boundary()
    t, real = 10.0, 3
    for i in range(20):
        gain += 24
        b.line(t, 22)
        b.bonus(2)
        real += 1
        if i % 4 == 0:
            gain += 24  # a kill whose line was missed, in the same step
            real += 1
        b.update(gain, t + 0.5, party=True)
        t += 1
    settle(b, t, gain)
    assert real == 28
    assert b.total == 23  # each step holds a read line: the missed kills share their steps


def test_a_kill_line_read_low_with_its_bonus_is_not_counted_twice():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    for i in range(3, 9):  # "+22" read as "+2", its "+2" bonus read right
        gain += kill(b, i, v=2, bonus=22, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    settle(b, 9.5, gain)
    assert b.total == 9


def test_a_kill_line_read_low_without_its_bonus_is_one_kill():
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    t = 3
    for _ in range(6):  # "+22" read as "+2", its bonus line missed (too far apart to be re-reads)
        gain += kill(b, t, v=2, bonus=22)
        b.update(gain, t + 0.5, party=True)
        t += b.repeat_s + 1
    settle(b, t, gain)
    assert b.total == 9


def test_a_bonus_line_read_low_is_still_one_kill():
    b, gain = BarKills(), 0
    for i in range(27):  # "+12" read as "+1" after the first three
        gain += kill(b, i, v=120, bonus=12, read_bonus=12 if i < 3 else 1)
        b.update(gain, i + 0.5, party=True)
    settle(b, 27.5, gain)
    assert b.total == 27


def test_a_misread_line_with_no_step_near_it_counts_once():
    # A kill line read with a digit dropped and no bar step near it (the bar
    # was unsure): the line is a kill, never more than one.
    b, gain = BarKills(), 0
    gain += kill(b, 0, v=200, bonus=20, read_bonus=20)
    b.update(gain, 0.5, party=True)
    gain += kill(b, 5, v=20, bonus=20, read_bonus=2)  # "+200" as "+20", "+20" as "+2"
    b.update(gain, 5.2, party=True, hold=True)
    settle(b, 10, gain)
    assert b.total == 2


def test_a_step_above_any_pair_is_one_kill():
    # A bigger mob's kill whose line was missed: one step, one kill, whatever
    # its EXP (no // U).
    b, gain = BarKills(), 0
    for i in range(3):
        gain += kill(b, i, v=20, read_bonus=2)
        b.update(gain, i + 0.5, party=True)
    gain += kill(b, 5, v=1000, bonus=100, read=False)
    b.update(gain, 5.5, party=True)
    settle(b, 6, gain)
    assert b.total == 4


def test_kill_and_bonus_exp_in_two_frames_are_one_step():
    b = BarKills()
    b.line(0, 20)
    b.bonus(2)
    b.shown(0)
    b.update(20, 0.2, party=True)
    b.update(22, 0.4, party=True)  # the bonus lands a frame later: the same step
    b.update(22, 7, party=True)
    assert b.total == 1


def test_a_step_with_no_line_near_it_is_not_a_kill():
    b = BarKills()
    b.update(500, 0, party=True)  # a quest's EXP: no EXP line on screen
    b.update(500, 20, party=True)
    assert b.total == 0 and b.quarantined == 500


def test_a_suspect_line_matched_to_a_step_counts_nothing_extra():
    # A line with no bonus line repeating a value just read may be a re-read.
    b = BarKills()
    b.line(0, 20)
    b.bonus(2)
    b.shown(0)
    b.update(22, 0.5, party=True)
    b.line(3, 20)  # "+20" again, no bonus: suspect
    b.shown(3)
    b.update(22, 10, party=True)  # no step near it
    assert b.total == 1
    b.line(12, 20)
    b.bonus(2)  # a real kill, its bonus line read
    b.shown(12)
    b.update(44, 12.5, party=True)
    b.update(44, 19, party=True)
    assert b.total == 2


def test_a_bonus_line_read_late_belongs_to_no_line():
    b = BarKills()
    b.line(0, 20)
    b.bonus(2, now=0.2)
    b.line(3, 20)
    b.bonus(2, now=4.0)  # another kill's bonus line, its kill line missed
    assert b._lines[0][3] and not b._lines[1][3]
