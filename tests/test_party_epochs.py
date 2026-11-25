"""Party kills from the EXP bar by epoch: lines + (gain no line paid for) // U."""
from mctracker.kills import BarKills, LineDiff
from mctracker.session import Session
from mctracker.vision.reader import TextLine

KILL, BONUS = "You received EXP (+22)", "Bonus EXP for PARTY (+2)"


def party_kill(b, t, read=True, bonus=True):
    if read:
        b.line(t, 22)
        if bonus:
            b.bonus(2)
            b.pair(24, 2)


def test_bar_counts_kills_whose_lines_were_missed_once_lagged():
    b = BarKills()
    gain = 0
    for i in range(10):  # 10 kills of 24 EXP, only the first 4 lines read
        party_kill(b, i, read=i < 4)
        gain += 24
        b.update(gain, i + 0.5, party=True)
    assert b.total == 4  # the bar's extra 6 kills wait for lag_s
    b.update(gain, 9 + b.lag_s + 1, party=True)
    assert b.total == 10


def test_bar_never_counts_more_than_the_largest_pair_allows():
    b = BarKills()
    b.pair(30, 3)  # a larger pair seen once (another monster)
    for i in range(3):
        party_kill(b, i)
    b.update(240, 3, party=True)  # 240 EXP: 10 kills of 24, or 8 of 30
    b.update(240, 3 + b.lag_s + 1, party=True)
    assert b.total <= 10 and b.total == 3 + (240 - 3 * 24) // 30


def test_floor_waits_for_three_pairs():
    b = BarKills()
    party_kill(b, 0)
    party_kill(b, 1)
    b.update(24 * 8, 1.5, party=True)
    b.update(24 * 8, 1.5 + b.lag_s + 1, party=True)
    assert b.total == 2  # two pairs: U isn't trusted yet, only the lines count


def test_a_larger_pair_starts_a_new_epoch():
    b = BarKills()
    for i in range(3):
        party_kill(b, i)
    b.update(72, 3, party=True)
    b.line(4, 44)
    b.bonus(4)
    b.pair(48, 4)  # someone left: the share doubled
    assert b._pairs == 1 and b._U == 48 and b.total == 3


def test_line_without_bonus_repeating_a_value_costs_a_whole_kill():
    b = BarKills()
    for i in range(3):
        party_kill(b, i)
    b.update(72, 3, party=True)
    b.line(4, 22)  # no bonus line, same value as before: maybe a re-read
    assert b._cost(b._lines[-1]) == 24  # U, so it adds nothing over gain // U


def test_kill_lines_without_bonus_end_party_kills():
    s = Session()
    s.set_party_size(2)
    s.on_message(_msg(KILL), now=0)
    s.on_message(_msg(BONUS), now=0)
    assert s._party_now(1)
    for t in (2, 4, 6):
        s.on_message(_msg(KILL), now=t)  # solo kills: no bonus lines
    assert s._party_now(6.5)  # the last one's bonus line may still come
    assert not s._party_now(7.5)
    assert s.killer.party[-3:] == [False] * 3  # their values are solo values


def test_widget_alone_ends_party_kills():
    s = Session()
    s.set_party_size(2)
    s.on_message(_msg(KILL), now=0)
    s.on_message(_msg(BONUS), now=0)
    s.party_widget_alone()
    assert not s._party_now(1) and s.party_size == 2  # the size label may hold


def test_rows_hidden_by_failed_reads_are_not_new_again():
    d = LineDiff()
    rows = [TextLine(t, y, 1.0, 10, 10, 20) for t, y in ((KILL, 56), (BONUS, 78))]
    assert len(d.update(rows, 0)) == 2
    for i in range(13):  # fast reads failing
        d.update([], 0.2 * i)
    assert d.update(rows, 3) == []  # first read 3 s ago: it can't have faded


def test_same_pair_after_a_fade_is_new():
    d = LineDiff()
    rows = [TextLine(t, y, 1.0, 10, 10, 20) for t, y in ((KILL, 56), (BONUS, 78))]
    d.update(rows, 0)
    for i in range(20):
        d.update([], 8 + 0.2 * i)  # faded
    assert len(d.update(rows, 14)) == 2  # the same kill again, at the same rows


def _msg(text):
    from mctracker.kills import parse_message
    return parse_message(text)
