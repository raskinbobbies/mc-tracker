"""Regression: kills counted that the player never made.

The fixture is the real OCR output of the messages area while solo on The
Burnt Land V: two kills (+43 each, the EXP bar went up 86). Before the fix the
same two lines were counted 9 times (saved session: 7 kills for 86 EXP).
Kills now come from the EXP bar only; lines give the mix (kills.BarKills).
"""

import json
from pathlib import Path

from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.kills import BarKills, LineDiff, Message, Mob, decompose, parse_message, split
from mctracker.session import Tracker
from mctracker.vision.reader import TextLine

FIXTURE = Path(__file__).parent / "fixtures" / "burnt_land_v_messages.txt"
BURNT_LAND = [Mob(20, "Dark Axe Stump", 43), Mob(21, "Axe Stump", 30)]
EXP43 = "You received EXP (+43)"


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


def bar(tr, exp, now):
    tr.on_status(45, exp, bar_percent(exp, EXP_TABLE[45]), now=now)


def replay(tr, fr, bar_every=3):
    tr.on_map("Warning Street : The Burnt Land V", BURNT_LAND)
    for i, (t, exp, lines) in enumerate(fr):
        tr.on_message_lines(lines, now=t)  # same order as the capture loop
        if i % bar_every == 0:  # the bar is read every ~1 s
            bar(tr, exp, t)
    bar(tr, fr[-1][1], fr[-1][0] + 1)
    return tr.session


def test_fixture_line_diff_samples_each_message():
    d = LineDiff()
    new = [m for _, _, lines in frames() for m in d.update(lines)]
    assert [m.value for m in new if m.kind == "exp"] == [43, 43]
    assert [m.kind for m in new].count("loot") == 1


def test_fixture_kills_match_exp_bar(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    s = replay(tr, frames())
    assert s.exp.gained == 86
    assert s.total_kills == 2 and s.kills == {20: 2} and s.unassigned_kills == 0
    rec = json.loads(tr.end_session().read_text())
    assert rec["party_kills"] == 2 and rec["party_size"] == 1


def test_fixture_with_lines_read_many_times_never_overcounts(tmp_path):
    # Every frame's lines doubled and re-shuffled: the lines over-count, the bar doesn't.
    fr = frames()
    for i, (_, _, lines) in enumerate(fr):
        if i % 2:
            lines[:] = lines + [TextLine(EXP43, 100 + i, 1.0, 220, 220, 22)]
    s = replay(Tracker(save_dir=tmp_path), fr)
    assert s.kills_read > 2
    assert s.total_kills <= 2


# --- line diff: covered or garbled bottom line ----------------------------------

def L(text, y):
    return TextLine(text, y, 1.0, 220.0, 220.0, 21.0)


def exp_values(d, reads):
    return [m.value for r in reads for m in d.update(r) if m.kind == "exp"]


def test_new_kill_under_damage_number():
    reads = [
        [L(EXP43, 273)],
        [L(EXP43, 252), TextLine("1074", 268, 1.0, 40, 40, 116)],  # bottom covered
        [L(EXP43, 252), L(EXP43, 273)],
        [L(EXP43, 252), L(EXP43, 273)],
    ]
    assert exp_values(LineDiff(), reads) == [43, 43]


def test_new_kill_first_read_as_fragment():
    reads = [[L(EXP43, 273)], [L(EXP43, 252), L("ved EXP (+43)", 273)], [L(EXP43, 252), L(EXP43, 273)]]
    assert exp_values(LineDiff(), reads) == [43, 43]


def test_line_hidden_for_a_frame_is_not_new():
    d = LineDiff()
    d.update([L(EXP43, 252), L(EXP43, 273)])
    assert d.update([L(EXP43, 252)]) == []
    assert d.update([L(EXP43, 252), L(EXP43, 273)]) == []


def test_garbled_loot_name_is_not_new():
    d = LineDiff()
    assert len(d.update([L("Pumpkin Piece x1 earned. (Etc)", 273)])) == 1
    assert d.update([L("Purpkin Piece xl eamed. (Etc)", 252)]) == []


# --- kills from the bar ----------------------------------------------------------

def test_decompose_fewest_kills():
    assert decompose(86, [43, 30]) == (2, 86)
    assert decompose(103, [43, 30]) == (3, 103)
    assert decompose(87, [43], tol=2) == (2, 86)
    assert decompose(50, [43]) == (0, 0)


def test_split_rounds_down_rest_unassigned():
    assert split({1: 2, 2: 1}, 4) == {1: 2, 2: 1, None: 1}
    assert split({1: 3, None: 1}, 2) == {1: 1, None: 1}
    assert split({}, 3) == {None: 3}


def test_duplicate_lines_capped_by_bar(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Burnt Land V", BURNT_LAND)
    bar(tr, 100_000, 0)
    tr.on_message_lines([(EXP43, 273)], now=1)
    for _ in range(3):  # the same kill read three more times
        tr.session.on_message(Message("exp", 43), now=1)
    bar(tr, 100_043, 2)
    s = tr.session
    assert s.kills_read == 4 and s.total_kills == 1 and s.kills == {20: 1}


def test_gain_without_exp_line_is_quarantined():
    b = BarKills()
    assert b.update(500, now=0, solo_values=[43]) == 0  # a quest: no kill line nearby
    b.update(500, now=b.nearby_s + 1, solo_values=[43])
    assert b.total == 0 and b.quarantined == 500
    b.line(now=20)
    assert b.update(543, now=20, solo_values=[43]) == 1  # later kills still count


def test_gain_not_a_sum_of_kill_values_is_quarantined():
    b = BarKills()
    b.line(now=0)
    assert b.update(50, now=0, solo_values=[43]) == 0
    b.update(50, now=b.stale_s + 1, solo_values=[43])
    assert b.total == 0 and b.quarantined == 50


def test_bar_gain_falling_never_uncounts():
    b = BarKills()
    b.line(now=0)
    b.update(86, now=0, solo_values=[43])
    b.update(43, now=1, solo_values=[43])  # a misread taken back
    assert b.update(86, now=2, solo_values=[43]) == 0  # back to the old high: no new kill
    assert b.total == 2


def test_party_divisor_from_kill_and_bonus_pairs(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Primeval Forest I", [Mob(54, "Glowshroom", 82)])
    bar(tr, 100_000, 0)
    tr.on_message_lines([("You received EXP (+33)", 252), ("Bonus EXP for PARTY (+6)", 273)], now=1)
    s = tr.session
    assert list(s.bar.per_kill) == [39]
    bar(tr, 100_000 + 39 * 3, 2)  # three kills; two lines scrolled past unread
    assert s.total_kills == 2  # 117 / (39 * margin), rounded down
    assert s.kills_read == 1


# --- party detection -------------------------------------------------------------

def test_party_messages_parse():
    assert parse_message("You have created a new party.") == Message("party", 0, "you", "created")
    assert parse_message("Bob has joined the party.") == Message("party", 0, "Bob", "joined")
    assert parse_message("You have left the party.") == Message("party", 0, "you", "left")
    assert parse_message("The party has been disbanded.").category == "disbanded"


def test_party_size_from_widget_and_chat(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    assert tr.party_size == 1 and tr.party_label == "Solo"
    tr.on_party(["raskinbobbi"], now=0)  # in a party, alone
    assert tr.party_size == 1
    tr.on_chat_lines([("Bob has joined the party.", 273)], now=1)
    assert tr.party_label == "Party of 2"
    tr.on_party(["raskinbobbi", "Bob", "Cat"], now=3)
    assert tr.party_size == 2  # one read isn't enough to grow
    tr.on_party(["raskinbobbi", "Bob", "Cat"], now=4.5)
    assert tr.party_size == 3
    for t in (6, 9):  # widget gone, but one or two misses aren't enough
        tr.on_party([], now=t)
        assert tr.party_size == 3
    tr.on_party([], now=12)  # three reads in a row: left the party
    assert tr.party_size == 1


def test_party_events_in_system_messages_are_ignored(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_message_lines([("Bob has joined the party.", 273)], now=1)
    assert tr.party_size == 1


def test_party_bonus_line_implies_party(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Primeval Forest I", [Mob(54, "Glowshroom", 82)])
    tr.on_message_lines([("You received EXP (+33)", 252), ("Bonus EXP for PARTY (+6)", 273)], now=0)
    assert tr.party_size == 2 and tr.session.party_size == 2
    assert tr.session.killer.party == [True]


def test_leaving_beats_a_recent_party_bonus(tmp_path):
    # Regression: the bonus-line hold kept us "in a party" after we left.
    tr = Tracker(save_dir=tmp_path)
    tr.on_party(["raskinbobbi", "Ahbabo"], now=0)
    tr.on_message_lines([("You received EXP (+33)", 252), ("Bonus EXP for PARTY (+6)", 273)], now=1)
    tr.on_chat_lines([("You have left the party.", 273)], now=2)
    assert tr.party_size == 1
    tr.on_party(["raskinbobbi", "Ahbabo"], now=3)  # stale widget read just after leaving
    for t in range(9, 30, 3):
        tr.on_party([], now=t)
    assert tr.party_size == 1


def test_solo_ignores_party_flags():
    from mctracker.session import Session

    s = Session(mobs=(Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98)))
    assert s.killer.solo
    s.killer.add(82, party=True)
    s.killer.mark_last_party()
    assert s.killer.party == [False]
    assert s.killer.counts() == {54: 1}
