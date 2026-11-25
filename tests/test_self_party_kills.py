"""Self vs party kills from the colour of each "You received EXP" line (#30): white is your
own kill, yellow a party share, an unread colour neither. Undercount only, each way."""
import os

import numpy as np
import pytest

from mctracker.kills import BarKills, KillCounter, LineDiff, Message, Mob, visible_rows
from mctracker.session import Session
from mctracker.vision.reader import TextLine, msg_row_colour

BG = (12, 10, 8)  # dark scene behind the strip (RGB)
TRUNK = (150, 95, 40)  # the recording's tree trunk (RGB)


def row(rgb, h=21, w=200, bg=BG, glyph=0.12, edge=0.2, blend=0.5, seed=0):
    """A message row: full-colour glyph cores, anti-aliased edges blended with ``bg``. BGR."""
    rng = np.random.default_rng(seed)
    img = np.empty((h, w, 3), np.float32)
    img[:] = bg
    m = rng.random((h, w))
    img[m < glyph] = rgb
    img[(m >= glyph) & (m < glyph + edge)] = np.array(rgb) * blend + np.array(bg) * (1 - blend)
    return img[:, :, ::-1].astype(np.uint8).copy()


# -- reading the colour ------------------------------------------------------------------

@pytest.mark.parametrize("rgb, want", [
    ((255, 255, 255), "white"),
    ((250, 248, 244), "white"),  # sampled from the party recording
    ((255, 255, 0), "yellow"),  # the game's yellow, lossless
    ((238, 246, 100), "yellow"),  # sampled: video chroma pulls its blue up
    ((205, 211, 93), "yellow"),  # sampled, fading
    ((218, 218, 218), "white"),  # sampled, fading
])
def test_message_row_colours(rgb, want):
    assert msg_row_colour(row(rgb)) == want
    assert msg_row_colour(np.dstack([row(rgb), np.full((21, 200), 255, np.uint8)])) == want  # BGRA


@pytest.mark.parametrize("img", [
    row((150, 150, 150)),  # faded white: too dim to tell
    row((150, 155, 55)),  # faded yellow
    row((255, 175, 175)),  # salmon (not a kill line colour)
    row((255, 153, 0)),  # orange
    row((210, 140, 60), bg=TRUNK),  # scenery, no text
    np.zeros((21, 200, 3), np.uint8),
    np.zeros((0, 0, 3), np.uint8),
], ids=["faded-white", "faded-yellow", "salmon", "orange", "trunk", "empty", "none"])
def test_unclear_rows_are_unknown(img):
    assert msg_row_colour(img) is None


def test_a_row_half_white_half_yellow_is_unknown():
    img = np.hstack([row((255, 255, 255), w=100), row((255, 255, 0), w=100, seed=1)])
    assert msg_row_colour(img) is None


def test_faded_white_over_the_trunk_is_never_yellow():
    """Over the brown trunk a fading white line tints orange: red pulls away from green, so
    it is never read as yellow (a self kill counted as a party share)."""
    for a in (0.3, 0.5, 0.7):
        rgb = tuple(255 * a + c * (1 - a) for c in TRUNK)
        assert msg_row_colour(row(rgb, bg=TRUNK, blend=0.8)) != "yellow"


# -- carrying it to the message ------------------------------------------------------------

def tl(text, y, colour=None):
    return TextLine(text, y, 1.0, 10.0, 200.0, 21.0, colour)


def test_joined_fragments_keep_a_colour_only_if_they_agree():
    same = visible_rows([tl("You received", 100, "white"), TextLine("EXP (+25)", 100, 1, 120, 200, 21, "white")])
    split = visible_rows([tl("You received", 100, "white"), TextLine("EXP (+25)", 100, 1, 120, 200, 21, "yellow")])
    assert same[0][3] == "white" and split[0][3] is None


def test_line_diff_gives_a_new_message_its_row_colour():
    d = LineDiff()
    out = d.update([tl("You received EXP (+25)", 100, "white"), tl("Bonus EXP for PARTY (+8)", 121, "yellow")], 0.0)
    assert [(m.kind, m.colour) for m in out] == [("exp", "white"), ("party_bonus", "yellow")]
    assert Message("exp", 25, colour="white") == Message("exp", 25)  # colour never decides sameness


# -- counting ----------------------------------------------------------------------------------

def party_run(kills, end=30.0):
    """BarKills in a party: ``kills`` is [(time, value, colour, line read?, bonus read?)].
    Each kill's EXP lands on the bar at its time; its lines are read then (if read)."""
    k = BarKills()
    k.update(0, 0.0, party=True)
    gain, bar_t = 0, None
    for n in range(int(end / 0.2)):
        t = round(n * 0.2, 3)
        for kt, v, c, line, bonus in kills:
            if abs(kt - t) < 1e-9:
                if line:
                    k.line(t, v, colour=c)
                    if bonus:
                        k.bonus(max(1, v // 10), t)
                gain, bar_t = gain + v + max(1, v // 10), t
        k.shown(t)
        k.update(gain, t, party=True, bar_t=bar_t if n % 5 else t)
    return k


def test_trusted_lines_are_split_by_colour():
    k = party_run([(1.0, 100, "white", True, True), (3.0, 40, "yellow", True, True),
                   (5.0, 60, None, True, True), (7.0, 80, "yellow", True, True)])
    assert k.total == 4
    assert (k.by["white"], k.by["yellow"], k.by["unknown"], k.by["step"]) == (1, 2, 1, 0)


def test_a_kill_whose_line_was_missed_is_never_a_party_kill():
    """A bar step with no line read may be one of your own kills (its white line missed): it
    counts in the total only, never as a party kill."""
    k = party_run([(1.0, 100, "white", False, False), (9.0, 40, "yellow", True, True)])
    assert k.total == 2 and k.by["step"] == 1 and k.by["yellow"] == 1 and k.by["white"] == 0


def test_a_white_line_read_again_is_one_self_kill_at_most():
    """The same white line read twice without its bonus (a re-read) is suspect the second
    time: one kill, one self kill."""
    k = party_run([(1.0, 100, "white", True, False), (2.0, 0, "white", False, False)], end=12)
    k.line(2.0, 100, colour="white")  # the re-read (no EXP of its own)
    for n in range(80):
        k.update(110, 12 + n * 0.2, party=True, bar_t=12 + n * 0.2)
    assert k.total <= 1 and k.by["white"] <= 1


def test_colour_never_changes_the_total():
    def run(c):
        return party_run([(1.0 + 2 * i, 50 + 7 * i, c, i % 3 != 0, i % 2 == 0) for i in range(10)])
    assert run("white").total == run("yellow").total == run(None).total


# -- per mob -------------------------------------------------------------------------------------

MOBS = [Mob(1, "Small", 100), Mob(2, "Mid", 300), Mob(3, "Top", 1000)]


def test_a_white_line_keeps_its_own_kill_flag_after_its_bonus_line():
    k = KillCounter(mobs=MOBS)
    k.add(300, party=True, colour="white")
    k.mark_last_party()  # its bonus line
    k.add(50, party=False, colour="yellow")  # a share whose bonus line was missed
    assert k.party == [False, True] and k.own == [True, False]


def test_white_lines_are_classified_only_with_a_trusted_fit():
    k = KillCounter(mobs=MOBS)
    for v in (100, 100, 300, 300, 1000):
        k.add(v, party=True, colour="white")
    k.add(40, party=True, colour="yellow")
    assert k.identify(300, party=False) == 2
    one = KillCounter(mobs=MOBS)
    one.add(300, colour="white")
    one.add(300, colour="white")
    one.add(40, party=True, colour="yellow")
    assert one.identify(300, party=False) is None  # one cluster can fit any scale


def test_session_splits_kills_per_mob(tmp_path):
    s = Session(mobs=MOBS, party_size=2)
    for v in (100, 100, 300, 300):
        s.killer.add(v, colour="white")
    s.bar.counted += [(300, True, "white"), (300, True, "white"), (900, True, "yellow"), (900, True, None),
                      (40, True, "yellow"), (300, False, None)]
    s.bar.by.update({"white": 2, "yellow": 2, "unknown": 1, "solo": 1, "step": 3})
    split = s.kills_split
    assert split[2] == {"self": 3}  # 2 white + 1 solo-rule line
    assert split[3] == {"party": 1, "unknown": 1}  # top-mob window
    assert (s.self_kills, s.party_share_kills, s.unknown_share_kills) == (3, 2, 4)


# -- the monsters table ------------------------------------------------------------------------------

def test_monster_table_shows_self_and_party(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from mctracker.gui import MonsterTable

    QApplication.instance() or QApplication([])

    class NoAssets:
        def mob_sprite(self, mob_id, cb):
            pass

    t = MonsterTable(NoAssets())
    # A party: the estimate per mob, "~own" with a small "+shared" beneath (as maplestory.watch).
    t.show_mobs(MOBS, {2: 3, 3: 2}, 120, {2: {"total": 3, "own": 3, "shared": 0},
                                          3: {"total": 5, "own": 1, "shared": 3}})
    cells = [t.cellWidget(r, 4).text() for r in range(3)]
    assert ["~0" in cells[0], "~3<br>" in cells[1], "~1<br>" in cells[2], "+3</span>" in cells[2]] == [True] * 4
    assert "1 not told apart" in t.cellWidget(2, 4).toolTip()
    assert t.item(2, 5).text() == "~2.5"  # per minute from the mob's ~total
    t.show_mobs(MOBS, {2: 3, 3: 2}, 120)  # solo: unchanged
    assert [t.item(r, 4).text() for r in range(3)] == ["0", "3", "2"]
    assert t.cellWidget(0, 4) is None


def test_a_pair_read_again_as_new_is_no_second_self_kill():
    """A row that failed to read, then showed again as new with its bonus line (probe rowfail
    seed 255): trusted, so the total may take it, but it repeats a value read 8.8 s before,
    so it goes to unknown, never a second self (or party) kill."""
    k = party_run([(1.0, 100, "white", True, True)], end=9.8)
    k.line(9.8, 100, colour="white")  # the same message, read again with its bonus
    k.bonus(10, 9.8)
    for n in range(80):
        k.update(110, 10 + n * 0.2, party=True, bar_t=10 + n * 0.2)
    assert k.by["white"] == 1


def test_kills_of_one_value_and_colour_minutes_apart_both_count_as_self():
    k = party_run([(1.0, 100, "white", True, True), (20.0, 100, "white", True, True)], end=40)
    assert k.by["white"] == 2


def _est(lines):
    """BarKills fed party lines (t, value, colour, slot, late) -> its estimate's line count."""
    k = BarKills()
    k.update(0, 0.0, party=True)
    for t, v, c, slot, late in lines:
        k.line(t, v, late, None, c, slot)
    k.settle_estimate()
    return dict(k.est)


def test_estimate_dedupes_a_reread_at_its_slot_but_counts_a_repeat_kill():
    # The same value at its slot 4 s later is that line read again; at the next row it is a new kill.
    assert _est([(0, 25, "white", 10.0, False), (4, 25, "white", 10.2, False)]) == {"white": 1}
    assert _est([(0, 25, "white", 10.0, False), (4, 25, "white", 11.0, False)]) == {"white": 2}
    # Past est_s it counts again (the old line has faded).
    assert _est([(0, 25, "white", 10.0, False), (13, 25, "white", 10.0, False)]) == {"white": 2}


def test_estimate_dedupes_a_late_line_a_row_off_and_a_misread_digit():
    # A late read (a lost scroll) a row off its slot: a re-read; not late, a new kill.
    assert _est([(0, 13, "yellow", 5.0, False), (2, 13, "yellow", 6.0, True)]) == {"yellow": 1}
    # 2500 misread as 250 (a dropped digit) at its slot, or late anywhere: one kill.
    assert _est([(0, 2500, "yellow", 5.0, False), (1, 250, "yellow", 5.0, False)]) == {"yellow": 1}
    assert _est([(0, 2500, "yellow", 5.0, False), (1, 200, None, 40.0, True)]) == {"yellow": 1}


def test_estimate_never_under_the_floor_and_solo_is_exact(tmp_path):
    from mctracker.session import largest_remainder
    from collections import Counter

    assert largest_remainder(10, Counter({1: 1, 2: 1, 3: 1})) == Counter({1: 4, 2: 3, 3: 3})
    assert sum(largest_remainder(7, Counter({5: 27, 6: 18, 9: 13})).values()) == 7
    k = BarKills()
    k.total, k.by["solo"] = 5, 5
    assert k.estimate() == {"total": 5, "own": 5, "shared": 0, "floor": 5}
