"""Round-4 review: misreads that repeat on a still bar. The reviewer's scripts
n1-n7 are tests here, plus one per finding and a property test."""

import random

from mctracker.exp_table import EXP_TABLE as T
from mctracker.exp_table import bar_percent as bp
from mctracker.exp_tracker import ExpTracker, Reading
from mctracker.session import Session

CONF = {"0": "86", "1": "7", "3": "8", "5": "6", "6": "58", "7": "1", "8": "306", "9": "8"}


def R(t, level, exp, pct="auto"):
    return Reading(t, level, exp, bp(exp, T[level]) if pct == "auto" else pct)


def pos(level, exp):
    return sum(T[lv] for lv in range(1, level)) + exp


class Feed:
    """Feeds a Session (or a bare tracker) with a clock and tracks the true gain."""

    def __init__(self, level, exp, kill, session=True, dt=1):
        self.s = Session() if session else None
        self.t = self.s.exp if session else ExpTracker()
        self.level, self.e, self.kill, self.now, self.dt = level, exp, kill, 0, dt
        self.start = None

    def read(self, level=None, exp=None, pct="auto"):
        level = self.level if level is None else level
        exp = self.e if exp is None else exp
        pct = bp(self.e, T[self.level]) if pct == "auto" else pct
        if self.s is not None:
            self.s.on_exp(level, exp, pct, now=self.now)
        else:
            self.t.add(Reading(self.now, level, exp, pct))
        self.now += self.dt

    def grind(self, n):
        for _ in range(n):
            self.e += self.kill
            if self.e >= T[self.level]:
                self.e -= T[self.level]
                self.level += 1
            if self.start is None:
                self.start = pos(self.level, self.e)
            self.read()

    @property
    def true(self):
        return pos(self.level, self.e) - self.start


# --- finding 1: a spike read twice is taken back ---------------------------------------------


def _pct_spike(level, e0, q, static):
    kill = int(T[level] * 0.005)
    pre = min(30, e0 // kill - 1)  # stay at EXP >= 0
    f = Feed(level, e0 - pre * kill, kill)
    f.grind(pre)
    assert f.e == e0
    for _ in range(static):
        f.read(pct=q)  # still bar, same wrong percent
    f.grind(12)
    return f


def test_n5_percent_misread_fitting_a_much_higher_level_read_1_to_4_times():
    # L32 at 10772 (8.77%) read as 0.77% fits L63: +19M and +31 levels before.
    for static in (1, 2, 3, 4):
        f = _pct_spike(32, 10_772, 0.77, static)
        assert f.t.fits(R(0, 32, 10_772, 0.77)) == [63]
        assert f.t.levels_gained == 0 and f.s.level == 32
        assert f.t.gained == f.true and f.s.best_minute() <= f.true


def test_n5_percent_misread_two_levels_up_read_1_to_4_times():
    # L55 at 98.63% read as 88.63% fits L57: stuck at level 57 before.
    for static in (1, 2, 3, 4):
        f = _pct_spike(55, 913_956, 88.63, static)  # the grind after it crosses into 56
        assert f.t.levels_gained == 1 and f.s.level == 56
        assert f.t.gained == f.true


def test_n6_trace_never_leaves_the_level():
    t = ExpTracker()
    kill = int(T[32] * 0.005)
    e = 10_772 - 5 * kill
    i = 0
    seq = []
    for _ in range(5):
        e += kill
        seq.append(R(0, 32, e))
    seq += [R(0, 32, e, 0.77)] * 2
    for _ in range(8):
        e += kill
        seq.append(R(0, 32, e))
    for r in seq:
        t.add(Reading(i, r.level, r.exp, r.pct))
        i += 1
        assert t.level == 32 and t.levels_gained == 0
        assert t.gained <= 13 * kill
    assert t.gained == 12 * kill and not t.behind


def test_n4_percent_misreads_found_by_search_never_change_the_level():
    tr = ExpTracker()
    rnd = random.Random(3)
    cases = []
    while len(cases) < 40:
        lvl = rnd.randint(25, 60)
        n = T[lvl]
        e = rnd.randint(n // 5, n - 1)
        ps = f"{bp(e, n):.2f}"
        for i, c in enumerate(ps):
            for d in CONF.get(c, ""):
                q = float(ps[:i] + d + ps[i + 1:])
                f = tr.fits(Reading(0, lvl, e, q))
                if len(f) == 1 and f[0] != lvl:
                    cases.append((lvl, e, q))
    for lvl, e0, q in cases:
        kill = int(T[lvl] * (0.02 if rnd.random() < 0.5 else 0.005))
        f = Feed(lvl, e0 - 10 * kill, kill, dt=3)
        f.grind(10)
        for _ in range(6):
            f.read(pct=q)
        f.grind(10)
        assert f.t.levels_gained == f.level - lvl and f.s.level == f.level, (lvl, e0, q)
        assert f.t.gained == f.true, (lvl, e0, q)


# --- finding 2: a persistent drop to a lower level is never followed ---------------------------


def test_n1_static_exp_misread_that_fits_a_lower_level():
    # L82 197200 read as 187200 (fits L81) six times: 10k inflated before.
    f = Feed(82, 190_000, 120)
    f.grind(60)
    mis = int(str(f.e).replace("19", "18", 1))
    assert f.t.fits(R(0, 82, mis, bp(f.e, T[82]))) == [81]
    for _ in range(6):
        f.read(exp=mis)
    assert f.t.behind and f.t.level == 82
    f.grind(10)
    assert f.t.gained == f.true and f.t.levels_gained == 0


def test_n2_exp_misreads_that_fit_a_lower_level():
    for lvl, e0, mis, lower in ((53, 193_955, 183_955, 52), (89, 3_845_402, 3_645_402, 88), (67, 888_005, 88_005, 35)):
        assert ExpTracker().fits(R(0, lvl, mis, bp(e0, T[lvl]))) == [lower]
        f = Feed(lvl, e0 - 6000, 120)
        f.grind(50)
        assert f.e == e0
        for _ in range(6):
            f.read(exp=mis)
        f.grind(10)
        assert f.t.levels_gained == f.level - lvl and f.t.gained == f.true, lvl


def test_n2_search_lower_level_exp_misreads_held_five_times():
    tr = ExpTracker()
    rnd = random.Random(2)
    cases = []
    while len(cases) < 30:
        lvl = rnd.randint(30, 89)
        n = T[lvl]
        e = rnd.randint(n // 50, n - 1)
        p, es = bp(e, n), str(e)
        for i, c in enumerate(es):
            for d in CONF.get(c, ""):
                m = int(es[:i] + d + es[i + 1:])
                f = tr.fits(Reading(0, lvl, m, p))
                if len(f) == 1 and f[0] < lvl:
                    cases.append((lvl, e, m))
    for lvl, e0, m in cases:
        f = Feed(lvl, e0, 0)
        f.read()
        f.kill = 120
        f.grind(5)
        e = f.e
        for _ in range(5):
            f.read(exp=m - e0 + e, pct=bp(e, T[lvl]))  # a still bar, misread the same way
        f.grind(5)
        assert f.t.levels_gained == f.level - lvl and f.t.gained == f.true + 120, (lvl, e0, m)


def test_lower_level_fit_is_never_booked():
    # A lower fit is never a plausible step. Persisting over 5 distinct readings
    # it re-anchors (rule 3), booking nothing for the move itself.
    t = ExpTracker()
    for i in range(6):
        t.add(R(i, 45, 100_000 + 100 * i))
    for i in range(4):
        t.add(R(10 + i, 44, 100_500 + i, bp(100_500 + i, T[44])))
        assert t.level == 45 and t.gained == 500 and t.behind
    for i in range(4, 20):
        t.add(R(10 + i, 44, 100_500 + i, bp(100_500 + i, T[44])))
    assert t.level == 44 and t.gained == 500 + 15 and t.levels_gained == 0 and t.lost == 0


# --- rules A and B on their own ----------------------------------------------------------------


def test_stale_digits_never_block_a_plausible_level_up():
    t = ExpTracker()
    t.add(R(0, 30, T[30] - 500))
    t.add(R(1, 30, T[30] - 400))
    # The fit says L31, the digits still say 30: rule 4, the digits don't block it.
    t.add(Reading(2, 30, 1000, bp(1000, T[31])))
    assert t.level == 31 and t.levels_gained == 1 and t.gained == 100 + 400 + 1000 and not t.behind
    t.add(Reading(3, None, 1100, bp(1100, T[31])))  # unreadable digits pass through
    assert t.gained == 100 + 400 + 1100


def test_level_up_with_unreadable_digits_is_a_plus_one_only():
    t = ExpTracker()
    t.add(R(0, 30, T[30] - 500))
    t.add(R(1, 30, T[30] - 400))
    t.add(Reading(2, 0, 1000, bp(1000, T[31])))
    assert t.level == 31 and t.levels_gained == 1


def test_big_same_level_rise_needs_a_changed_frame():
    t = ExpTracker()
    t.add(R(0, 45, 100_000))
    t.add(R(1, 45, 100_100))
    for i in range(6):  # +20% of the level, read identically on a still bar
        t.add(R(2 + i, 45, 200_000))
    assert t.gained == 100 and t.behind
    for i in range(3):  # the frame changes and agrees: real (a quest), re-anchor
        assert t.gained == 100
        t.add(R(10 + i, 45, 200_300 + 100 * i))
    assert t.gained == 100 and not t.behind and t.last.exp == 200_500  # quest not booked
    t.add(R(20, 45, 200_600))
    assert t.gained == 200


def test_big_rise_that_returns_to_the_base_is_dropped():
    t = ExpTracker()
    t.add(R(0, 45, 100_000))
    t.add(R(1, 45, 100_100))
    for i in range(6):
        t.add(R(2 + i, 45, 200_000))
    t.add(R(10, 45, 100_200))
    assert t.gained == 200 and not t.behind


# --- finding 3: a town death of 1% ---------------------------------------------------------------


def test_n3_one_percent_and_five_percent_deaths_are_booked():
    for lvl, frac in ((45, 100), (89, 100), (70, 100), (89, 20)):
        n = T[lvl]
        pen = n // frac
        f = Feed(lvl, n // 2, 200)
        f.grind(30)
        gained = f.true
        f.e -= pen
        for _ in range(8):
            f.read()
        f.grind(5)
        assert f.t.lost == pen, (lvl, frac)
        assert f.t.gained == gained + 1000, (lvl, frac)


# --- finding 4: behind while the percent is unreadable --------------------------------------------


def test_n7_behind_while_the_percent_is_unreadable():
    s = Session()
    e = 100_000
    for i in range(5):
        e += 500
        s.on_exp(45, e, bp(e, T[45]), now=100 + i)
    for i in range(20):
        e += 500
        s.on_exp(45, e, None, now=105 + i)
    t = s.exp
    assert t.behind
    assert t.verified_gain == 2000
    assert t.last_committed_t == 104  # monotonic, like message timestamps


# --- finding 5: interface ---------------------------------------------------------------------------


def test_last_committed_t_is_monotonic_time_not_the_active_clock():
    s = Session()
    s.on_exp(45, 100_000, bp(100_000, T[45]), now=5_000.0)
    s.on_exp(45, 100_100, bp(100_100, T[45]), now=5_001.0)
    assert s.exp.last.t != 5_001.0  # readings carry the active clock
    assert s.exp.last_committed_t == 5_001.0


def test_seed_stamps_the_caller_clock():
    t = ExpTracker()
    t.seed(Reading(0.0, 45, 100_000, bp(100_000, T[45])), now=777.0)
    assert t.last_committed_t == 777.0


def test_verified_gain_never_goes_down_and_excludes_an_unconfirmed_spike():
    t = ExpTracker()
    seq = [(45, 100_000), (45, 100_100), (45, 140_000), (45, 140_000), (45, 100_200), (45, 100_300), (45, 100_400)]
    seen = []
    for i, (lvl, e) in enumerate(seq):
        t.add(R(i, lvl, e))
        seen.append(t.verified_gain)
    assert seen == sorted(seen)
    assert max(seen) <= 400 and t.gained == 400
    assert t.verified_gain + t.unconfirmed_gain == t.gained


def test_spike_read_twice_is_measured_from_the_pre_spike_base():
    # Rule C: the repeat of the spike never becomes the base.
    t = ExpTracker()
    for i, e in enumerate((100_000, 100_100, 140_000, 140_000, 140_000)):
        t.add(R(i, 45, e))
    assert t.previous is not None and t.previous.exp == 100_100
    t.add(R(5, 45, 100_200))  # still a correction after the repeats
    assert t.gained == 200 and t.undone == 39_900


# --- property: one misread, repeated on a still bar -------------------------------------------------


def _misread(rnd, r):
    fields = []
    for name, text in (("level", str(r.level)), ("exp", str(r.exp)), ("pct", f"{r.pct:.2f}")):
        for i, c in enumerate(text):
            for d in CONF.get(c, ""):
                fields.append((name, text[:i] + d + text[i + 1:]))
    if not fields:
        return None
    name, text = rnd.choice(fields)
    if name == "level":
        return Reading(r.t, int(text), r.exp, r.pct)
    if name == "exp":
        return Reading(r.t, r.level, int(text), r.pct)
    return Reading(r.t, r.level, r.exp, float(text))


def _true_readings(rnd):
    lvl = rnd.randint(20, 85)
    e = rnd.randint(T[lvl] // 20, T[lvl] - 1)
    kill = max(1, int(T[lvl] * rnd.uniform(0.001, 0.03)))
    out = []
    for _ in range(120):
        if rnd.random() < 0.4:
            e += kill
            if e >= T[lvl]:
                e -= T[lvl]
                lvl += 1
        out.append((lvl, e))
    return out


def test_property_one_repeated_misread_on_a_still_bar_changes_nothing():
    rnd = random.Random(4)
    tried = 0
    while tried < 400:
        truth = _true_readings(rnd)
        at = rnd.randint(30, 100)
        lvl, e = truth[at]
        frame = R(0, lvl, e)
        mis = _misread(rnd, frame)
        if mis is None:
            continue
        tried += 1
        reps = rnd.randint(1, 6)
        clean, dirty = ExpTracker(), ExpTracker()
        verified = []
        i = 0
        for k, (lv, ex) in enumerate(truth):
            clean.add(R(i, lv, ex))
            dirty.add(R(i, lv, ex))
            i += 1
            if k == at:
                for _ in range(reps):  # the bar is still: the true frame, misread
                    clean.add(R(i, lv, ex))
                    dirty.add(Reading(i, mis.level, mis.exp, mis.pct))
                    verified.append(dirty.verified_gain)
                    i += 1
            verified.append(dirty.verified_gain)
        case = (truth[at], mis, reps)
        assert dirty.levels_gained == clean.levels_gained, case
        assert abs(dirty.gained - clean.gained) <= T[lvl] * 0.01, case
        assert verified == sorted(verified), case
