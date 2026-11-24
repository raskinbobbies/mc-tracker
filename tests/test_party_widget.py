"""Party widget: members at low HP or dead still count (review finding 3).

The widget is rebuilt synthetically from measurements of a real 2-member
party screenshot (party region 316x219 px at 2000x1125): each member's HP bar
is a black outline, a 1-2 px white border, a red fill from the left and a
translucent grey track for the missing HP.
"""

import numpy as np
import pytest

from mctracker.session import PARTY_DROP_READS, Tracker
from mctracker.vision.reader import hp_bar_rows

BG = (94, 108, 88)  # BGR, the widget's translucent panel over a forest map


def widget(hps, h=219, w=316, top=27, pitch=27):
    """Party region with one HP bar per entry of ``hps`` (fraction 0..1)."""
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = BG
    for i, hp in enumerate(hps):
        y = top + i * pitch
        img[y:y + 17, 191:294] = 0  # black outline
        img[y + 1:y + 15, 192:293] = 255  # white border
        img[y + 3:y + 13, 194:291] = (70, 60, 66)  # grey track
        fill = int(round(97 * hp))
        img[y + 3:y + 13, 194:194 + fill] = (0, 0, 220)  # red HP
    return img


@pytest.mark.parametrize("hp", [1.0, 0.6, 0.3, 0.1, 0.03, 0.0])
def test_low_hp_member_still_a_row(hp):
    assert len(hp_bar_rows(widget([1.0, hp]))) == 2


def test_full_party_of_six_at_mixed_hp():
    assert len(hp_bar_rows(widget([1.0, 0.0, 0.5, 0.02, 0.8, 0.0]))) == 6


def test_no_widget_no_rows():
    img = np.zeros((219, 316, 3), np.uint8)
    img[:] = BG
    assert hp_bar_rows(img) == []


def test_white_box_is_not_a_bar():
    # A window title bar (white box, grey border) in the region isn't a member.
    img = widget([])
    img[100:120, 0:316] = (200, 200, 200)
    img[102:118, 2:314] = 255
    assert hp_bar_rows(img) == []


# --- party size hysteresis ---------------------------------------------------------

def test_party_size_drops_only_after_consecutive_reads(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(["raskinbobbi", "Ahbabo"], now=0)
    assert tr.party_size == 2
    t = 0
    for _ in range(PARTY_DROP_READS - 1):  # a member missed for a few reads
        t += 3
        tr.on_party(["raskinbobbi"], now=t)
        assert tr.party_size == 2
    tr.on_party(["raskinbobbi", "Ahbabo"], now=t + 3)  # back: the count restarts
    for _ in range(PARTY_DROP_READS - 1):
        t += 3
        tr.on_party(["raskinbobbi"], now=t + 3)
    assert tr.party_size == 2
    tr.on_party(["raskinbobbi"], now=t + 6)
    assert tr.party_size == 1


def test_party_size_grows_at_once(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(["a"], now=0)
    tr.on_party(["a", "b", "c"], now=3)
    assert tr.party_size == 3


def test_left_message_drops_at_once(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(["raskinbobbi", "Ahbabo"], now=0)
    tr.on_message_lines([("Ahbabo has left the party.", 273)], now=1)
    assert tr.party_size == 1


def test_disbanded_message_drops_at_once(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(["raskinbobbi", "Ahbabo", "Cat"], now=0)
    tr.on_message_lines([("The party has been disbanded.", 273)], now=1)
    assert tr.party_size == 1


def test_party_bonus_keeps_party_of_two(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(["raskinbobbi", "Ahbabo"], now=0)
    tr.on_message_lines([("You received EXP (+33)", 252), ("Bonus EXP for PARTY (+6)", 273)], now=1)
    for t in range(3, 3 + 3 * (PARTY_DROP_READS + 2), 3):  # Ahbabo unreadable
        tr.on_party(["raskinbobbi"], now=t)
    assert tr.party_size == 2
