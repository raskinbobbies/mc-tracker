"""Party widget: members at low HP or dead still count (review finding 3).

The widget is rebuilt synthetically from measurements of a real 2-member
party screenshot (party region 316x219 px at 2000x1125): each member's HP bar
is a black outline, a 1-2 px white border, a red fill from the left and a
translucent grey track for the missing HP.
"""

import numpy as np
import pytest

from mctracker.kills import Message, parse_message
from mctracker.session import PARTY_DROP_READS, PARTY_RAISE_READS, PARTY_SETTLE_S, Tracker
from mctracker.vision import reader as reader_mod
from mctracker.vision.reader import REGIONS, GameState, find_party_widget, hp_bar_rows

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
    tr.on_party(2, now=-3)
    tr.on_party(2, now=0)
    assert tr.party_size == 2
    t = 0
    for _ in range(PARTY_DROP_READS - 1):  # a member missed for a few reads
        t += 3
        tr.on_party(1, now=t)
        assert tr.party_size == 2
    tr.on_party(2, now=t + 3)  # back: the count restarts
    for _ in range(PARTY_DROP_READS - 1):
        t += 3
        tr.on_party(1, now=t + 3)
    assert tr.party_size == 2
    tr.on_party(1, now=t + 6)
    assert tr.party_size == 1


def test_party_size_grows_after_consecutive_reads(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(1, now=0)
    t = 0
    for _ in range(PARTY_RAISE_READS - 1):
        t += 3
        tr.on_party(3, now=t)
        assert tr.party_size == 1
    tr.on_party(2, now=t + 3)  # agreeing reads: the smaller size wins
    assert tr.party_size == 2


def test_one_false_widget_read_stays_solo(tmp_path):
    # Review finding: a single misread (damage numbers) labelled a solo session
    # PARTY KILLS for good (party_max never comes down).
    tr = Tracker(save_dir=tmp_path)
    for t in range(0, 60, 3):
        tr.on_party(2 if t % 9 == 0 else 0, now=t)  # misread every 3rd read
    assert tr.party_size == 1


def test_left_message_drops_at_once(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(2, now=0)
    tr.on_chat_lines([("Ahbabo has left the party.", 273)], now=1)
    assert tr.party_size == 1


def test_disbanded_message_drops_at_once(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(3, now=0)
    tr.on_chat_lines([("The party has been disbanded.", 273)], now=1)
    assert tr.party_size == 1


def test_party_bonus_keeps_party_of_two(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(2, now=0)
    tr.on_message_lines([("You received EXP (+33)", 252), ("Bonus EXP for PARTY (+6)", 273)], now=1)
    for t in range(3, 3 + 3 * (PARTY_DROP_READS + 2), 3):  # Ahbabo unreadable
        tr.on_party(1, now=t)
    assert tr.party_size == 2


# --- finding the widget anywhere (it's a movable window) ----------------------------

def frame_with_widget(x, y, hps, h=1125, w=2000):
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = (40, 90, 60)
    wid = widget(hps)
    frame[y:y + wid.shape[0], x:x + wid.shape[1]] = wid
    frame[900:920, 100:400] = 255  # a white box elsewhere is not a bar
    return frame


@pytest.mark.parametrize("x,y", [(1680, 264), (40, 500), (900, 30)])
def test_widget_found_anywhere(x, y):
    box = find_party_widget(frame_with_widget(x, y, [1.0, 0.2, 0.0]))
    assert box is not None and abs(box[0] - x) <= 3 and abs(box[1] - y) <= 3
    x0, y0, x1, y1 = box
    assert len(hp_bar_rows(frame_with_widget(x, y, [1.0, 0.2, 0.0])[y0:y1, x0:x1])) == 3


def test_no_widget_not_found():
    frame = np.zeros((1125, 2000, 3), np.uint8)
    frame[900:920, 100:400] = 255
    assert find_party_widget(frame) is None


def test_widget_with_star_rows_beats_a_bare_bar_column():
    frame = frame_with_widget(40, 500, [1.0, 0.5])
    frame[500 + 30:500 + 40, 40 + 30:40 + 40] = (40, 180, 250)  # leader star (BGR orange)
    decoy = widget([1.0, 1.0, 1.0])  # more bars, no star rows
    frame[100:100 + decoy.shape[0], 1500:1500 + decoy.shape[1]] = decoy
    x, y = find_party_widget(frame)[:2]
    assert abs(x - 40) <= 3 and abs(y - 500) <= 3


def test_chat_party_lines_with_timestamps():
    from mctracker.kills import Message, parse_message

    assert parse_message("[18:08] 'Ahbabo' has joined the party.") == Message("party", 0, "Ahbabo", "joined")
    assert parse_message("[19:12] You have left the party.") == Message("party", 0, "you", "left")
    assert parse_message("[19:13] Bob : lol I left the party") is None


@pytest.mark.parametrize("line,expected", [
    ("[18:08] 'Ahbabo' has joined the party.", ("joined", "Ahbabo")),
    ("[18:08]'Ahbabo' has joined the party", ("joined", "Ahbabo")),
    ("(18:08) Ahbabo has joined the party.", ("joined", "Ahbabo")),
    ("[19:12] You have created a new party.", ("created", "you")),
    ("[19:12] You have joined the party.", ("joined", "you")),
    ("[19:12] You have left the party.", ("left", "you")),
    ("19:12] You have left the party.", ("left", "you")),  # "[" lost: still you, not a member
    ("(19:12) You have left the party.", ("left", "you")),
    ("[19.12 You have left the party", ("left", "you")),
    ("[19:12] 'Ahbabo' has left the party.", ("left", "Ahbabo")),
    ("[19:12] The party has been disbanded.", ("disbanded", "you")),
    ("[19:12] You have been expelled from the party.", ("left", "you")),
    ("[19:12] 'Ahbabo' has been expelled from the party.", ("left", "Ahbabo")),
])
def test_chat_party_sentences(line, expected):
    m = parse_message(line)
    assert m is not None and (m.category, m.item) == expected


@pytest.mark.parametrize("line", [
    "[18:08] raskinbobbi: I left the party",  # player chat, no space before ":"
    "[18:08] raskinbobbi : You have left the party.",
    "[18:08] Bob: 'Ahbabo' has joined the party.",
    "[18:08] sSilent() : the party has been disbanded lol",
    "[18:08] wait the party has been disbanded?",
    "[18:08] disbanded",
    "[18:08] guys I have left the party",
    "[18:08] raskinbobbi : thx",
])
def test_player_chat_is_not_a_party_event(line):
    assert parse_message(line) is None


# --- solo: red on the play field isn't a party (review finding) --------------------

def play_field_with_damage_numbers(h=1125, w=2000):
    """Open play field where the widget would be, with red/orange damage
    numbers: thin, wide, saturated blobs that a red-fill check took for rows."""
    rng = np.random.default_rng(1)
    frame = rng.integers(30, 120, (h, w, 3), dtype=np.uint8)
    x0, y0 = int(REGIONS["party"][0] * w), int(REGIONS["party"][1] * h)
    for i, color in enumerate([(0, 0, 230), (20, 120, 250), (0, 30, 200), (0, 0, 255)]):
        y = y0 + 25 + 40 * i
        frame[y:y + 10, x0 + 140:x0 + 290] = color  # a row of digits, merged
        frame[y + 2:y + 8, x0 + 150:x0 + 280:12] = (255, 255, 255)  # glyph highlights
    return frame


def test_damage_numbers_in_party_region_are_not_rows():
    from mctracker.vision.reader import crop

    assert hp_bar_rows(crop(play_field_with_damage_numbers(), "party")) == []
    assert find_party_widget(play_field_with_damage_numbers()) is None


class _FakeReader(reader_mod.Reader):
    def __init__(self):  # no OCR model
        self.ocr = None
        self._party_box = None
        self._party_search_at = 0.0
        self._party_misses = 0
        self._party_rows = 0
        self._chat_sig = None

    def _lines(self, img, upscale=2.0):
        return []


def test_solo_reads_stay_solo_and_search_backs_off(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: clock[0])
    searches = []
    real = reader_mod.find_party_widget
    monkeypatch.setattr(reader_mod, "find_party_widget", lambda f: searches.append(clock[0]) or real(f))
    r, frame = _FakeReader(), play_field_with_damage_numbers()
    for t in range(0, 300, 3):
        clock[0] = float(t)
        s = GameState()
        r.read_party(frame, s)
        assert s.party == 0
    gaps = [b - a for a, b in zip(searches, searches[1:])]
    assert gaps[:2] == [9, 9] and all(g >= reader_mod.PARTY_SEARCH_SLOW_S for g in gaps[3:])


def test_stale_party_box_is_dropped(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: clock[0])
    r = _FakeReader()
    r.read_party(frame_with_widget(40, 500, [1.0, 0.5]), GameState())
    assert r._party_box is not None
    clock[0] = 10.0
    r.read_party(play_field_with_damage_numbers(), GameState())  # widget closed
    assert r._party_box is None


def test_widget_moved_partly_away_is_searched_again(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(reader_mod.time, "monotonic", lambda: clock[0])
    r = _FakeReader()
    s = GameState()
    r.read_party(frame_with_widget(40, 500, [1.0, 0.5, 0.2]), s)
    assert s.party == 3
    clock[0] = 10.0
    s = GameState()
    r.read_party(frame_with_widget(40, 670, [1.0, 0.5, 0.2]), s)  # dragged down: one row left in the old box
    assert s.party == 3 and abs(r._party_box[1] - 670) <= 3


# --- a video capture of the widget (#29) ------------------------------------------

def video_widget(hps):
    """``widget`` as a video capture shows it: chroma subsampling bleeds the
    red fill into the white edge next to it, so stretches of the edge come out
    pink-tinted (BGR values measured in the owner's 1080p party recording)."""
    img = widget(hps)
    for i, hp in enumerate(hps):
        y, fill = 27 + i * 27, int(round(97 * hp))
        img[y + 1:y + 3, 194 + fill // 3:194 + fill] = (227, 224, 255)  # top edge over the fill
        img[y + 13:y + 15, 194:194 + fill // 2] = (214, 214, 246)  # bottom edge
    return img


@pytest.mark.parametrize("hps", [[0.9], [0.9, 0.6], [0.9, 0.6, 1.0, 0.5], [1.0, 0.9, 0.6, 0.3, 1.0, 0.5]])
def test_tinted_bar_edges_still_rows(hps):
    assert len(hp_bar_rows(video_widget(hps))) == len(hps)


@pytest.mark.parametrize("hps", [[0.9], [0.9, 0.6], [0.9, 0.6, 1.0, 0.5], [1.0, 0.9, 0.6, 0.3, 1.0, 0.5]])
def test_tinted_widget_found_with_all_rows(hps):
    frame = np.zeros((1125, 2000, 3), np.uint8)
    frame[:] = (40, 90, 60)
    frame[264:264 + 219, 1680:1996] = video_widget(hps)
    box = find_party_widget(frame)
    assert box is not None and abs(box[0] - 1680) <= 3 and abs(box[1] - 264) <= 3
    x0, y0, x1, y1 = box
    assert len(hp_bar_rows(frame[y0:y1, x0:x1])) == len(hps)


def _tracker_in_party_of(n, tmp_path, monkeypatch):
    tr = Tracker(save_dir=tmp_path)
    tr._start(None, 0.0)
    tr.on_party(n, now=0.0)
    tr.on_party(n, now=3.0)
    assert tr.party_size == n
    boundaries = []
    monkeypatch.setattr(tr.session.bar, "boundary", lambda *a, **k: boundaries.append(a))
    return tr, boundaries


def test_one_frame_missing_rows_closes_no_epoch(tmp_path, monkeypatch):
    tr, boundaries = _tracker_in_party_of(4, tmp_path, monkeypatch)
    for t, n in ((6, 4), (9, 2), (12, 4), (15, 4), (18, 3), (21, 4)):  # single-read misses
        tr.on_party(n, now=t)
    assert tr.party_size == 4 and tr.session.party_size == 4 and boundaries == []


def test_party_size_change_needs_time_as_well_as_reads(tmp_path, monkeypatch):
    # Reads in a quick burst all agreeing on a smaller party aren't enough:
    # the count has to hold for PARTY_SETTLE_S.
    tr, boundaries = _tracker_in_party_of(4, tmp_path, monkeypatch)
    t = 3.0
    for _ in range(PARTY_DROP_READS + 2):
        t += 0.1
        tr.on_party(2, now=t)
    assert tr.party_size == 4 and boundaries == []
    tr.on_party(3, now=3.1 + PARTY_SETTLE_S + 0.05)  # held long enough: the largest count seen
    assert tr.party_size == 3 and tr.session.party_size == 3 and len(boundaries) == 1


def test_bigger_party_needs_time_too(tmp_path, monkeypatch):
    tr, boundaries = _tracker_in_party_of(2, tmp_path, monkeypatch)
    tr.on_party(3, now=3.1)
    tr.on_party(3, now=3.2)
    assert tr.party_size == 2 and boundaries == []
    tr.on_party(3, now=3.1 + PARTY_SETTLE_S + 0.05)
    assert tr.party_size == 3 and len(boundaries) == 1
