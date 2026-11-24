"""Regression: kills counted that the player never made.

The fixture is the real OCR output of the messages area while solo on The
Burnt Land V: two kills (+43 each, the EXP bar went up 86). Before the fix the
same two lines were counted 9 times (saved session: 7 kills for 86 EXP).
"""

import json
from pathlib import Path

from mctracker.kills import LineTracker, Message, Mob, credited_kills, parse_message
from mctracker.session import Tracker
from mctracker.vision.reader import TextLine

FIXTURE = Path(__file__).parent / "fixtures" / "burnt_land_v_messages.txt"
BURNT_LAND = [Mob(20, "Dark Axe Stump", 43), Mob(21, "Axe Stump", 30)]


def frames():
    """-> [(t, exp_bar, [TextLine])] from the fixture."""
    out = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        if raw.startswith("@"):
            _, t, exp = raw.split()
            out.append((float(t), int(exp.removeprefix("exp=")), []))
        else:
            y, h, x0, text = raw.split("\t", 3)
            out[-1][2].append(TextLine(text, float(y), 1.0, float(x0), float(x0), float(h)))
    return out


def test_fixture_line_tracker_reads_each_message_once():
    t = LineTracker()
    new = [parse_message(line) for _, _, lines in frames() for line in t.update(lines)]
    assert [m.kind for m in new] == ["exp", "exp", "loot"]
    assert [m.value for m in new if m.kind == "exp"] == [43, 43]


def test_fixture_session_matches_exp_bar(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Warning Street : The Burnt Land V", BURNT_LAND)
    for i, (t, exp, lines) in enumerate(frames()):
        tr.on_message_lines(lines, now=t)  # same order as the capture loop
        if i % 3 == 0:  # the bar is read every ~1 s
            tr.on_status(45, exp, 15.9, now=t)
    tr.on_status(45, frames()[-1][1], 15.9, now=20)
    s = tr.session
    assert s.exp.gained == 86
    assert s.kills_read == 2 and s.total_kills == 2
    assert s.kills == {20: 2}
    rec = json.loads(tr.end_session().read_text())
    assert rec["party_kills"] == 2 and rec["party_size"] == 1


# --- line tracker edge cases seen in the recording ---------------------------

EXP43 = "You received EXP (+43)"


def test_single_line_jitter_is_not_a_new_line():
    # One line on screen, OCR y wobbles by half a pixel.
    t = LineTracker()
    assert t.update([TextLine(EXP43, 272.5, 1, 0, 0, 21.5)]) == [EXP43]
    assert t.update([TextLine(EXP43, 273.0, 1, 0, 0, 23.0)]) == []
    assert t.update([TextLine("You received ExP (+43)", 272.8, 1, 0, 0, 22.0)]) == []


def test_line_hidden_for_a_frame_is_not_counted_again():
    t = LineTracker(line_height=21)
    two = [(EXP43, 252), ("Pumpkin Piece x1 earned. (Etc)", 273)]
    t.update([(EXP43, 231)] + two)
    t.update([("219", 120), ("Pumpkin Piece x1 earned. (Etc)", 273)])  # damage numbers on top
    assert t.update([(EXP43, 231)] + two) == []


def test_garbled_loot_name_is_the_same_line():
    t = LineTracker(line_height=21)
    t.update([(EXP43, 252), ("PumokyRiece xl eamed. (Eic)", 273)])
    assert t.update([(EXP43, 252), ("Pumpkin Piece xl eamed. (Etc)", 273)]) == []


# --- safety net: kills never exceed the EXP bar --------------------------------

def test_credited_kills_capped_by_bar():
    # 7 reads of +43 but the bar only gained 86.
    assert credited_kills([43] * 7, 86, verified=7) == 2
    # Kills read after the last bar reading aren't on the bar yet: allowed.
    assert credited_kills([43] * 3, 86, verified=2) == 3
    assert credited_kills([43] * 3, 0, verified=3, allowance=43) == 1


def test_session_caps_duplicates(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Burnt Land V", BURNT_LAND)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([(EXP43, 273)], now=1)
    for _ in range(3):  # the same kill read three more times
        tr.session.on_message(Message("exp", 43), now=1)
    tr.on_status(45, 1043, 10.1, now=2)
    s = tr.session
    assert s.kills_read == 4
    assert s.total_kills == 1
    assert s.kills == {20: 1}


def test_no_cap_while_bar_is_untrusted(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Burnt Land V", BURNT_LAND)
    tr.on_status(45, 1000, 99.0, now=0)
    tr.on_message_lines([(EXP43, 273)], now=1)
    tr.on_status(46, 5, 0.0, now=2)  # level-up, EXP table unknown
    assert tr.session.total_kills == 1


# --- party detection -------------------------------------------------------------

def test_party_messages_parse():
    assert parse_message("You have created a new party.") == Message("party", 0, "you", "created")
    assert parse_message("Bob has joined the party.") == Message("party", 0, "Bob", "joined")
    assert parse_message("You have left the party.") == Message("party", 0, "you", "left")
    assert parse_message("The party has been disbanded.").category == "disbanded"


def test_party_size_from_widget_and_messages(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    assert tr.party_size == 1 and tr.party_label == "Solo"
    tr.on_party(["raskinbobbi"], now=0)  # in a party, alone
    assert tr.party_size == 1
    tr.on_message_lines([("Bob has joined the party.", 273)], now=1)
    assert tr.party_label == "Party of 2"
    tr.on_party(["raskinbobbi", "Bob", "Cat"], now=3)
    assert tr.party_size == 3
    tr.on_party([], now=6)  # widget gone: left the party
    assert tr.party_size == 1


def test_party_bonus_line_implies_party(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Primeval Forest I", [Mob(54, "Glowshroom", 82)])
    tr.on_message_lines([("You received EXP (+33)", 252), ("Bonus EXP for PARTY (+6)", 273)], now=0)
    assert tr.party_size == 2
    assert tr.session.party_size == 2
    assert tr.session.killer.party == [True]


def test_solo_ignores_party_flags():
    from mctracker.session import Session

    s = Session(mobs=(Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98)))
    assert s.killer.solo
    s.killer.add(82, party=True)
    s.killer.mark_last_party()
    assert s.killer.party == [False]
    assert s.killer.counts() == {54: 1}
