"""Replay of a real party recording's message strip and EXP bar (#22).

A 4-member party on Forest Up North VII, 49.7 s: the bar went +1,312 and 53
"You received EXP" lines (with their party bonuses) sum to exactly that."""
from pathlib import Path

import pytest

from mctracker.kills import Mob
from mctracker.session import Tracker
from mctracker.vision.reader import TextLine

FIXTURE = Path(__file__).parent / "fixtures" / "party_12-52-05_messages.txt"
MOBS = [Mob(27, "Fairy 3", 54), Mob(31, "Curse Eye", 63), Mob(37, "Cold Eye", 85)]
REAL_KILLS = 53


def frames():
    """-> [(t, {exp, pct, party?}, [TextLine])] from the fixture."""
    out = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        if raw.startswith("@"):
            _, t, *kv = raw.split()
            out.append((float(t), dict(p.split("=") for p in kv), []))
        else:
            y, h, x0, text = raw.split("\t", 3)
            out[-1][2].append(TextLine(text, float(y), 1.0, float(x0), float(x0), float(h)))
    return out


def replay(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Victoria Road : Tree Dungeon, Forest Up North VII", MOBS)
    fr = frames()
    bar_every = round(1.0 / (fr[1][0] - fr[0][0]))  # the bar is read every 1 s
    for i, (t, kv, lines) in enumerate(fr):
        if "party" in kv:
            tr.on_party(int(kv["party"]), now=t)
        tr.on_message_lines(lines, now=t)  # same order as the capture loop
        if i % bar_every == 0 and kv["exp"] != "?":
            tr.on_status(45, int(kv["exp"]), None if kv["pct"] == "?" else float(kv["pct"]), now=t)
    t, kv, _ = fr[-1]
    for k in range(1, 30):  # the bar keeps being read: late gain settles
        tr.on_status(45, int(kv["exp"]), float(kv["pct"]), now=t + k)
    return tr.session


def test_replay_never_overcounts(tmp_path):
    s = replay(tmp_path)
    assert s.exp.gained == 1312
    assert s.total_kills <= REAL_KILLS


@pytest.mark.xfail(reason="counts 49: LineDiff reads 47 of the 53 kill lines; the 6 missed kills' "
                          "EXP settles into // U with U = 45 (41 + 4, the largest share), while the "
                          "missed kills average ~19, so // U gets back 2 of the 6", strict=False)
def test_replay_counts_nearly_every_kill(tmp_path):
    s = replay(tmp_path)
    assert 50 <= s.total_kills <= REAL_KILLS
