"""Round-6 review: four rules that can only ever undercount (see the
exp_tracker module doc).

The reviewer's a1-a5 scripts are tests here; u3.py (realistic grinding with
capture gaps) and fz7.py (misreads followed by unreadable frames, and
offsets that persist while the bar moves) are shortened into property tests."""

import math
import random

import pytest

import fuzz_misreads as F
from mctracker.exp_table import EXP_TABLE as T
from mctracker.exp_table import bar_percent as bp
from mctracker.exp_tracker import ExpTracker, Reading


def pos(level, exp):
    return sum(T[lv] for lv in range(1, level)) + exp


class Feed:
    def __init__(self):
        self.t = ExpTracker()
        self.i = 0

    def __call__(self, level, exp, pct="auto"):
        self.i += 1
        p = bp(exp, T[level]) if pct == "auto" else pct
        return self.t.add(Reading(self.i, level, exp, p))


def run(seq):
    f = Feed()
    for x in seq:
        f(*x)
    f.t.finalize()
    return f.t


# --- a1 / fix 2: a drop after a still-young step is judged from its base -------------------------


def test_a1_misread_rise_then_kill_is_not_a_death():
    n = T[50]
    k, c, x = n // 100, n * 40 // 100, int(n * 0.035)
    seq = [(50, c - 3 * k), (50, c - 2 * k), (50, c - k), (50, c), (50, c + x), (50, c + k), (50, c + k)]
    seq += [(50, c + j * k) for j in range(2, 7)]
    t = run(seq)
    assert t.lost == 0 and t.gained == 9 * k and t.levels_gained == 0


# --- a2 / fixes 2 and 3: unreadable or unfitting frames don't age a misread -----------------------


def _a2(level, c, k, mis, between, after_n=8):
    seq = [(level, c - 3 * k), (level, c - 2 * k), (level, c - k), (level, c), mis] + between
    e = c
    for _ in range(after_n):
        e += k
        seq.append((level, e))
    return run(seq), e - (c - 3 * k)


@pytest.mark.parametrize(
    "case",
    [
        "a: L50 +9% then 2 percent-None",
        "b: L20 misread level-up then 2 percent-None",
        "c: L30 +40% then 2 percent-None",
        "d: L50 +9% then 2 unfitting",
        "e: L60 +7%, kill 2%, one still frame",
    ],
)
def test_a2_misread_then_unvalidated_frames(case):
    if case[0] in "ad":
        n = T[50]
        k, c = n // 200, n * 40 // 100
        trail = None if case[0] == "a" else 12.34
        t, true = _a2(50, c, k, (50, c + int(0.09 * n)), [(50, c + k + 1, trail), (50, c + k + 2, trail)])
    elif case[0] == "b":
        n = T[20]
        k, c = n // 100, n // 2
        t, true = _a2(20, c, k, (21, T[21] // 5), [(20, c + k + 1, None), (20, c + k + 2, None)])
    elif case[0] == "c":
        n = T[30]
        k, c = n // 200, n // 4
        t, true = _a2(30, c, k, (30, c + int(0.4 * n)), [(30, c + k + 1, None), (30, c + k + 2, None)])
    else:
        n = T[60]
        k, c = n // 50, n * 30 // 100
        t, true = _a2(60, c, k, (60, c + int(0.07 * n)), [(60, c + k), (60, c + k)], after_n=6)
    assert t.levels_gained == 0 and t.lost == 0
    assert t.gained <= true and t.gained >= true - 2 * k
    assert t.verified_gain <= true


def test_unreadable_frames_never_confirm_a_step():
    f = Feed()
    for e in (100_000, 100_500):
        f(45, e)
    for e in (101_000, 101_500, 102_000):
        f(45, e, None)
    assert f.t.unconfirmed_gain == 500 and f.t.verified_gain == 0


# --- a4 / fix 3: a fake level after a misread pumped the cap -------------------------------------


def test_a4_two_stage_misread_books_no_level():
    n = T[50]
    k = n // 200
    f = Feed()
    e = int(n * 0.70)
    e0 = e + k
    for _ in range(10):
        e += k
        f(50, e)
    f(50, e + int(0.09 * n))
    f(50, e + 1, None)
    f(50, e + 2, None)
    for _ in range(10):
        e += k
        f(50, e)
    assert f.t.gained <= e - e0 and f.t.lost == 0
    assert f.t.step_cap(50, f.i) < 0.15 * n  # the misread never pumped the cap
    f(51, int(T[51] * 0.03))
    f(50, e + 1, None)
    f(50, e + 2, None)
    for _ in range(10):
        e += k
        f(50, e)
    assert f.t.levels_gained == 0 and f.t.gained <= e - e0
    while e + k < n:
        e += k
        f(50, e)
    ee = e + k - n
    for _ in range(5):
        ee += k
        f(51, ee)
    f.t.finalize()
    assert f.t.levels_gained == 1


def test_downward_reanchor_takes_back_and_reports_it():
    f = Feed()
    for i in range(6):
        f(45, 100_000 + 100 * i)
    for i in range(10):  # a lower level persisting: re-anchored below the booked position
        f(44, 100_500 + i)
    t = f.t
    assert t.reanchors == 1 and t.retracted == 500 and t.gained == 5 and t.levels_gained == 0
    vs = t.verified_gain
    assert vs >= 0  # verified_gain stays a high-water mark; retracted says how much came back out


# --- a5 / fix 1: a booked death is never reversed --------------------------------------------------


@pytest.mark.parametrize(
    "level, start, mis",
    [
        (50, 0.5, lambda ref, n: (50, ref + int(0.08 * n))),  # A: +8% above the pre-death position
        (50, 0.89, lambda ref, n: (51, int(T[51] * 0.01))),  # B: misread as L51 1%
        (70, 0.5, lambda ref, n: (70, ref + n // 200)),  # C: pre-death value +0.5%
    ],
)
def test_a5_misread_after_a_real_death_books_nothing(level, start, mis):
    n = T[level]
    k = n // 200
    f = Feed()
    e = int(n * start)
    e0 = e + k
    for _ in range(8):
        e += k
        f(level, e)
    ref, pen = e, n // 10
    e -= pen
    f(level, e)
    f(level, e)
    f(*mis(ref, n))
    for _ in range(12):
        e += k
        f(level, e)
    f.t.finalize()
    true_gained = (e - e0) + pen
    assert f.t.levels_gained == 0
    assert f.t.gained <= true_gained and f.t.lost <= pen
    assert f.t.net <= e - e0


# --- fix 4: capture gaps ----------------------------------------------------------------------------


def _grind(seed, gap):
    """u3.py, shortened: 1 s readings, Poisson kills of 0.1-2% of a level,
    rare deaths, and readings missing between gap[0] and gap[1]."""
    rnd = random.Random(seed)
    level = rnd.randint(26, 84)
    e = rnd.randint(0, T[level] - 1)
    ks, lph = rnd.uniform(0.001, 0.02), rnd.uniform(1, 8)
    lam = lph / 3600 / ks
    t, p0, lost = ExpTracker(), None, 0
    for i in range(900):
        x, nk = rnd.random(), 0
        p = c = math.exp(-lam)
        while x > c and nk < 50:
            nk += 1
            p *= lam / nk
            c += p
        for _ in range(nk):
            e += max(1, int(T[level] * ks * rnd.uniform(0.5, 1.5)))
            while e >= T[level]:
                e -= T[level]
                level += 1
        if rnd.random() < 0.0003 and e > 0:
            pen = min(e, T[level] // 10)
            e -= pen
            lost += pen
        if gap[0] <= i < gap[1]:
            continue
        t.add(Reading(i, level, e, bp(e, T[level])))
        if p0 is None:
            p0 = pos(level, e)
    t.finalize()
    return pos(level, e) - p0 + lost, t.gained


@pytest.mark.parametrize("gap, limit", [((300, 360), 0.01), ((300, 420), 0.02)])
def test_capture_gaps_lose_little(gap, limit):
    # The same grind with and without the gap: what the gap costs.
    true = clean = gapped = 0
    for seed in range(40):
        tg, g = _grind(seed, gap)
        tc, c = _grind(seed, (0, 0))
        assert tg == tc and g <= tg and c <= tc
        true, clean, gapped = true + tg, clean + c, gapped + g
    assert (clean - gapped) / true < limit


def test_gap_cap_is_bounded_by_a_level_and_never_multi_level():
    t = ExpTracker()
    for i in range(10):
        t.add(Reading(i, 50, 100_000 + 5_000 * i, bp(100_000 + 5_000 * i, T[50])))
    n = T[50]
    assert t.step_cap(50, 10, 1) < 0.15 * n
    assert t.step_cap(50, 200, 190) <= n  # a long gap allows at most one level
    e = 145_000 + int(0.3 * n)
    assert t.add(Reading(200, 50, e, bp(e, n))) > 0  # 30% after a 190 s gap: booked
    t2 = ExpTracker()
    for i in range(10):
        t2.add(Reading(i, 50, 100_000 + 5_000 * i, bp(100_000 + 5_000 * i, T[50])))
    e2 = int(T[52] * 0.3)
    t2.add(Reading(200, 52, e2, bp(e2, T[52])))  # two levels after a gap: never booked
    assert t2.levels_gained == 0


def test_rejected_repeats_never_widen_the_cap():
    # A quest reward read for 60 s is seen, not a gap: it stays unbooked.
    n = T[40]
    t = run([(40, 10_000 + 100 * i) for i in range(10)] + [(40, 10_900 + int(0.5 * n))] * 60)
    assert t.gained == 900


# --- fz7: misread then unreadable frames, and a persisting offset ----------------------------------


def _fz7(seed):
    rnd = random.Random(seed)
    band, frames = F._truth(rnd)
    at = rnd.randint(20, len(frames) - 40)
    level, e, _, _ = frames[at]
    kind = rnd.choice(["e", "p", "ep", "lep", "le", "lp"])
    bad = F._misreads(level, e, bp(e, T[level]), kind, rnd)
    if not bad:
        return None
    lv, me, mp = rnd.choice(bad)
    fl = F._FITTER.fits(Reading(0, lv, me, mp))
    if len(fl) != 1:
        return None
    shape = rnd.choice(["trail", "persist"])
    feed = [(lv_, x, bp(x, T[lv_])) for lv_, x, _, _ in frames]
    if shape == "trail":
        feed = feed[:at] + [(lv, me, mp), (level, e + 1, None), (level, e + 2, None)] + feed[at:]
    else:
        off = pos(fl[0], me) - pos(level, e)
        for j in range(at, at + rnd.randint(2, 4)):
            lj, xj, _ = feed[j]
            p, ml = pos(lj, xj) + off, 1
            while ml < 89 and p >= T[ml]:
                p -= T[ml]
                ml += 1
            if p < 0:
                break
            feed[j] = (ml, p, bp(p, T[ml]))
    t = ExpTracker()
    for i, (lv_, x, p) in enumerate(feed):
        t.add(Reading(i, lv_, x, p))
    t.finalize()
    f0, f1 = frames[0], frames[-1]
    return t, f1[2] - f0[2], f1[0] - f0[0], T[level]


def test_fz7_trailing_unreadable_and_persisting_offsets_never_inflate():
    for seed in range(300):
        res = _fz7(seed)
        if res is None:
            continue
        t, true_gain, true_levels, need = res
        assert t.levels_gained <= true_levels, seed
        assert t.gained <= true_gain + need // 100, seed
