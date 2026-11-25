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
            y, h, x0, rest = raw.split("\t", 3)
            text, _, colour = rest.partition("\t")  # a kill line may carry its colour (#30)
            out[-1][2].append(TextLine(text, float(y), 1.0, float(x0), float(x0), float(h), colour or None))
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


@pytest.mark.xfail(reason="counts 17 (#22 r4: was 42, r5: 21): the late rules that keep one kill from making "
                          "two trusted lines (misread slots, guessed scrolls, rejoins, young messages after "
                          "failed reads) mark many lines suspect; 45 if late and doubted lines were trusted",
                   strict=False)
def test_replay_counts_nearly_every_kill(tmp_path):
    s = replay(tmp_path)
    assert 50 <= s.total_kills <= REAL_KILLS


# The recording's kill lines by colour (#30): about 18 white (your own kills: +24, +25, +41)
# and 35 yellow (party shares), as issue #30 counted them on the video.
REAL_SELF, REAL_PARTY = 18, 35


def test_replay_splits_self_and_party_kills_without_overcounting_either(tmp_path):
    s = replay(tmp_path)
    assert s.self_kills + s.party_share_kills + s.unknown_share_kills == s.total_kills
    assert s.self_kills <= REAL_SELF and s.party_share_kills <= REAL_PARTY
    # Recall on this recording when #30 landed: 2 self, 2 party of the 17 counted (10 and 7
    # before a line repeating a value read within 12 s went to unknown, see decisions.md).
    assert s.self_kills >= 2 and s.party_share_kills >= 2


def test_replay_estimate_is_close_and_under_the_truth(tmp_path):
    """The watch-style estimate (2026-11-26 "Changed:" entry): every distinct kill line, re-reads
    deduped. On this recording it reads 42 of 53 (15 own of ~18, 26 shared of ~35); the proven
    floor stays 17."""
    s = replay(tmp_path)
    s.finish()
    e = s.kills_estimate
    assert e["floor"] == s.total_kills <= e["total"] <= REAL_KILLS
    assert e["own"] <= REAL_SELF and e["shared"] <= REAL_PARTY
    assert e["total"] >= 40 and e["own"] >= 13 and e["shared"] >= 24
