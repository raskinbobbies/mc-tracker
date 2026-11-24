"""Round-5 review: book only plausible movement (see the exp_tracker module doc).

The reviewer's scripts are tests here: inf.py (finding 1), g.py/g2.py
(findings 2, 3, 5, 6), trk.py (findings 3, 4), low2.py (low-level fast
leveling) and fz.py (the fuzz, extended in fuzz_misreads.py)."""

import json
import random

import pytest

from fuzz_misreads import report, run_many
from mctracker.exp_table import EXP_TABLE as T
from mctracker.exp_table import bar_percent as bp
from mctracker.exp_tracker import CANDIDATE_CONFIRM, CONTRADICT_CONFIRM, ExpTracker, Reading
from mctracker.kills import Mob
from mctracker.session import Tracker


def pos(level, exp):
    return sum(T[lv] for lv in range(1, level)) + exp


def r(level, exp, digits="same", pct="auto"):
    return (level if digits == "same" else digits, exp, bp(exp, T[level]) if pct == "auto" else pct)


def run(seq, fin=True):
    t = ExpTracker()
    for i, x in enumerate(seq):
        t.add(Reading(i, *x))
    if fin:
        t.finalize()
    return t


def true_gain(seq_truth):
    (l0, e0), (l1, e1) = seq_truth[0], seq_truth[-1]
    return pos(l1, e1) - pos(l0, e0)


# --- finding 1 (inf.py): combined misreads never book levels or extra EXP -------------


def test_inf_a_level_and_percent_misread_on_a_still_bar():
    e = 3_131_063
    seq = [r(80, e - 5000 * (10 - i)) for i in range(10)] + [r(80, e), (82, e, 80.02)]
    seq += [r(80, e)] * 3 + [r(80, e + 5000 * i) for i in range(1, 4)]
    t = run(seq)
    assert t.levels_gained == 0 and t.gained == 65_000


@pytest.mark.parametrize("reps", [1, 2])
def test_inf_b_level_and_percent_misread_on_a_moving_bar(reps):
    seq = [r(11, 1000 + 80 * i) for i in range(10)] + [(21, 1803, 7.40)] * reps
    seq += [r(11, 1803 + 80 * i) for i in range(1, 6)]
    t = run(seq)
    assert t.levels_gained == 0 and t.gained == 1803 + 400 - 1000


def test_inf_c_level_and_exp_misread_ten_times():
    e = 425_672
    seq = [r(59, e - 3000 * (10 - i)) for i in range(10)] + [r(59, e)] + [(69, 725_672, 37.11)] * 10
    seq += [r(59, e + 3000 * i) for i in range(1, 4)]
    t = run(seq)
    assert t.levels_gained == 0 and t.gained == 39_000


# --- finding 3 (g.py G1, trk.py T2): quests are not booked, by design -----------------

N40 = T[40]
K40 = int(N40 * 0.01)
E40 = int(N40 * 0.30)


@pytest.mark.parametrize(
    "grind, quest",
    [
        ([E40 + i * K40 for i in range(10)], int(N40 * 0.25)),  # G1: rate window not yet full
        ([E40 + (i // 10) * K40 for i in range(120)], int(N40 * 0.25)),  # G1c: slow grind
        ([E40 + (i // 30) * K40 for i in range(120)], int(N40 * 0.50)),  # G1d
        ([E40] * 30, int(N40 * 0.50)),  # G1e: still bar
    ],
)
def test_quest_turn_in_is_never_booked(grind, quest):
    seq = [r(40, e) for e in grind] + [r(40, grind[-1] + quest)] * 60
    t = run(seq, fin=False)
    assert t.behind  # the quest reading is pending, never booked
    assert t.finalize() == 0
    assert t.gained == grind[-1] - grind[0] and t.levels_gained == 0


def test_quest_then_grinding_re_anchors_and_keeps_counting():
    seq = [r(40, E40 + i * K40) for i in range(10)]
    top = E40 + 9 * K40 + int(N40 * 0.4)
    seq += [r(40, top + i * K40) for i in range(10)]
    t = run(seq)
    # 9 kills before, the quest skipped, and the kills after the re-anchor
    # (CANDIDATE_CONFIRM readings are spent confirming it).
    assert t.gained == (9 + 9 - CANDIDATE_CONFIRM) * K40 and t.reanchors == 1


def test_trk_t2_quest_in_hunting_map_then_leave(tmp_path):
    n = T[40]
    e = int(n * 0.3)
    tr = Tracker(save_dir=tmp_path)
    _go(tr, [(40, e + (i // 6) * 500) for i in range(120)] + [(40, e + 19 * 500 + int(n * 0.4))] * 20)
    assert tr.last.exp.gained == 19 * 500 and tr.last.exp.levels_gained == 0


# --- finding 2 (g.py G2, g2.py G2g): the level digits never block a plausible level-up ---

L30_SEQ = [r(30, T[30] - 500 - 1000 + i * 100) for i in range(10)]
L30_TRUE_TO_END = 1000 + 500  # from the first reading to the end of level 30


@pytest.mark.parametrize("digits", [37, 30, None, 0])
def test_level_up_with_misread_digits_is_booked_at_once(digits):
    seq = L30_SEQ + [r(31, 200 + i * 300, digits=digits) for i in range(20)]
    t = run(seq, fin=False)
    assert t.levels_gained == 1 and t.gained == L30_TRUE_TO_END + 200 + 19 * 300 and not t.behind


def test_g2e_g2f_low_level_ups_with_bad_digits():
    seq = [r(9, 1000 + i * 20) for i in range(10)] + [r(10, 50 + i * 30, digits=1) for i in range(5)]
    t = run(seq)
    assert t.levels_gained == 1 and t.gained == pos(10, 170) - pos(9, 1000)
    seq = [r(9, 1000 + i * 20) for i in range(10)] + [r(10, 50 + i * 30, digits=0) for i in range(5)]
    t = run(seq + [r(11, 10, digits=0)])
    assert t.levels_gained == 2 and t.gained == pos(11, 10) - pos(9, 1000)


def test_g2g_whole_level_misread_then_next_level():
    seq = [r(30, T[30] - 1000 + i * 100) for i in range(10)]
    seq += [r(31, i * 1000, digits=37) for i in range(T[31] // 1000)] + [r(32, 500)]
    t = run(seq)
    assert t.levels_gained == 2 and t.gained == pos(32, 500) - pos(30, T[30] - 1000)


def test_frozen_level_digits_and_stale_anchor_recover_within_5_readings():
    # The committed reading is stale (the bar was read wrong at the anchor):
    # rule 3 re-anchors after CONTRADICT_CONFIRM distinct consistent readings.
    seq = [r(45, 300_000)] * 3 + [r(45, 100_000 + 100 * i, digits=99) for i in range(12)]
    t = ExpTracker()
    for i, x in enumerate(seq):
        t.add(Reading(i, *x))
        if i == 2 + CONTRADICT_CONFIRM:
            assert t.last.exp < 300_000 and not t.behind  # recovered
    assert t.gained <= 1100 and t.lost == 0 and t.levels_gained == 0


@pytest.mark.parametrize("x", [0, 5, 30, 200, 2000])
def test_g3_level_up_then_session_ends(x):
    seq = [r(30, T[30] - 3000 + i * 200) for i in range(10)] + [r(31, x)] * 2
    t = run(seq)
    assert t.levels_gained == 1 and t.gained == pos(31, x) - pos(30, T[30] - 3000)


# --- findings 5 and 6 (g2.py D1, D2): deaths --------------------------------------------


@pytest.mark.parametrize("level, kf, df", [(10, 0.08, 0.10), (20, 0.09, 0.10), (30, 0.05, 0.05), (15, 0.12, 0.10)])
def test_kill_then_death_nets_out(level, kf, df):
    n = T[level]
    e, k, pen = int(n * 0.5), int(n * kf), int(n * df)
    seq = [r(level, e + i * k // 4) for i in range(8)]
    e2 = e + 7 * (k // 4)
    seq += [r(level, e2 + k)] + [r(level, e2 + k - pen)] * 5
    t = run(seq)
    assert t.net == 7 * (k // 4) + k - pen  # always right
    assert t.gained <= 7 * (k // 4) + k and t.lost <= pen
    if pen > k:  # the drop lands below the kill's start: clearly a death
        assert t.lost == pen


def test_kill_then_equal_death_is_a_correction_documented():
    # Finding 5: a kill then a death of the same size looks exactly like a
    # misread spike; it's taken back (net right, gained and lost both lower).
    n = T[30]
    k = pen = int(n * 0.05)
    t = run([r(30, 50_000 + i * 1000) for i in range(8)] + [r(30, 57_000 + k)] + [r(30, 57_000 + k - pen)] * 5)
    assert t.net == 7000 and t.lost == 0


def test_death_sized_misread_on_a_still_bar_is_taken_back():
    # A combined EXP+percent misread 4% down, read twice: booked as a death,
    # then the bar comes back: the death is reversed, nothing is inflated.
    n = T[60]
    e = int(n * 0.5)
    seq = [r(60, e + 1000 * i) for i in range(5)] + [r(60, e + 4000 - int(n * 0.04))] * 2
    seq += [r(60, e + 4000), r(60, e + 5000)]
    t = run(seq)
    assert t.lost == 0 and t.gained == 5000 and t.net == 5000


@pytest.mark.parametrize("nread", [1, 2])
def test_death_then_map_change(nread):
    n = T[40]
    e = int(n * 0.5)
    seq = [r(40, e + i * 500) for i in range(10)] + [r(40, e + 9 * 500 - int(n * 0.1))] * nread
    t = run(seq)
    assert t.lost == int(n * 0.1) and t.gained == 4500


# --- finding 4 (trk.py T1): the young anchor --------------------------------------------


def _go(tr, seq):
    tr.on_map("Field", [Mob(54, "Glowshroom", 82)])
    for i, (lv, e) in enumerate(seq):
        tr.on_status(lv, e, bp(e, T[lv]), now=float(i))
    return tr.end_session()


def test_trk_t1_low_level_start_big_kill_then_level_up(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    seq = [(7, 314)] * 3 + [(7, 523)] * 3 + [(8, 321)] + [(8, 321 + 30 * i) for i in range(1, 5)]
    rec = json.loads(_go(tr, seq).read_text())
    assert rec["levels_gained"] == 1 and rec["exp_gained"] == pos(8, 441) - pos(7, 314)


def test_misread_anchor_only_undercounts():
    # The first reading fits a wrong level; nothing is ever inflated.
    for first in [(46, 74_473), (44, 74_473), (45, 99_473)]:
        seq = [(first[0], first[1], bp(first[1], T[first[0]]))] + [r(45, 74_500 + 100 * i) for i in range(10)]
        t = run(seq)
        assert t.levels_gained == 0 and 0 <= t.gained <= 1000


# --- low2.py: low-level fast leveling ----------------------------------------------------


def _low_grind(seed, l0, kf, pk, n=300):
    rnd = random.Random(seed)
    lv, e, seq = l0, rnd.randint(0, T[l0] - 1), []
    for _ in range(n):
        if rnd.random() < pk:
            e += max(1, int(T[l0] * kf * rnd.uniform(0.7, 1.3)))
            while e >= T[lv]:
                e -= T[lv]
                lv += 1
        seq.append((lv, e, bp(e, T[lv])))
    return seq


def test_low_level_fast_leveling_books_kills():
    total = under = exact = 0
    for seed in range(150):
        rr = random.Random(seed * 7)
        l0, kf, pk = rr.randint(1, 25), rr.choice([0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8]), rr.choice([0.2, 0.5, 0.9])
        seq = _low_grind(seed, l0, kf, pk)
        t = run(seq)
        tg = pos(seq[-1][0], seq[-1][1]) - pos(seq[0][0], seq[0][1])
        assert t.gained <= tg and t.levels_gained <= seq[-1][0] - seq[0][0]
        total, under, exact = total + tg, under + tg - t.gained, exact + (t.gained == tg)
    assert under / total < 0.01 and exact >= 140


# --- the interface ---------------------------------------------------------------------


def test_a_contradicted_spike_never_reaches_verified_gain():
    t = ExpTracker()
    t.add(Reading(0, 45, 100_000, bp(100_000, T[45])))
    t.add(Reading(1, 45, 100_100, bp(100_100, T[45])))
    for i in range(10):  # a plausible-size spike read again and again on a still bar
        t.add(Reading(2 + i, 45, 140_000, bp(140_000, T[45])))
    assert t.verified_gain == 100 and t.unconfirmed_gain == 39_900
    t.add(Reading(20, 45, 100_200, bp(100_200, T[45])))  # the frame changes: back down
    assert t.gained == 200 and t.verified_gain <= 200 and t.lost == 0


def test_unreadable_level_digits_pass_through_the_session():
    from mctracker.session import Session

    s = Session()
    s.on_exp(45, 100_000, bp(100_000, T[45]), now=0)
    s.on_exp(None, 100_500, bp(100_500, T[45]), now=1)
    assert s.exp.gained == 500 and s.level == 45 and s.digits_level == 45


# --- the property fuzz -------------------------------------------------------------------


def test_fuzz_misreads_never_inflate_and_never_freeze():
    runs = run_many(400, seed0=1000)
    assert len(runs) > 300
    for x in runs:
        assert x.levels <= x.true_levels, x
        assert x.gained <= x.true_gain + x.need // 100, x
        assert x.net <= x.true_net + x.need // 100, x
        assert x.longest_stall <= 10, x
        assert x.verified_monotonic, x
    print(report(runs))
