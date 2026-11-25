"""#22: party kills through the real ``LineDiff`` -> ``BarKills`` and ``ExpTracker`` path.

The count must never pass the real kills. H1-H4 are the input faults the r4 review found
(one kill making two trusted lines or two steps); the property test replays its generators."""
import random

import pytest

from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.exp_tracker import ExpTracker, Reading
from mctracker.kills import BarKills, LineDiff

K = lambda v: f"You have received EXP (+{v})"  # noqa: E731
B = lambda v: f"Bonus EXP for PARTY (+{v})"  # noqa: E731
LV = 50
NEED = EXP_TABLE[LV]


def feed(d, k, frame, t):
    for m in d.update(frame, now=t):
        if m.kind == "exp":
            k.line(t, m.value, m.late, m.doubt)
        elif m.kind == "party_bonus":
            k.bonus(m.value, t)
    if "exp" in d.shown:
        k.shown(t)


def lines_only(frames, gain, n=80):
    d, k = LineDiff(), BarKills()
    k.update(0, 0.0, party=True)
    t = 0.0
    for i in range(n):
        t = round(t + 0.2, 3)
        feed(d, k, frames(i), t)
        k.update(gain(i), t, party=True)
    return k.total


def test_h1_a_misread_of_a_line_just_read_is_no_second_line():
    # One kill: its line, then misread +1000 -> +100 with its bonus, then read right.
    two = lambda i: [(K(1000), 100.0)] if i == 0 else (  # noqa: E731
        [(K(100 if i == 1 else 1000), 100.0), (B(50), 112.0)] if i < 40 else [])
    one = lambda i: [(K(1000), 100.0)] if i != 1 and i < 40 else ([(K(100), 100.0)] if i == 1 else [])  # noqa: E731
    for frames in (two, one):  # was 2; now the misread is late and the first read doubted: <= 1
        assert lines_only(frames, lambda i: 1050 if i >= 1 else 0) <= 1


@pytest.mark.parametrize("lo,hi", [(30, 34), (40, 44)])
def test_h2_lines_read_again_after_failed_reads_are_late(lo, hi):
    frames = lambda i: [] if lo <= i <= hi or i > 50 else [(K(1000), 100.0), (B(50), 112.0)]  # noqa: E731
    assert lines_only(frames, lambda i: 1050, n=120) == 1


def bar_only(seq, line_at=6):
    e, k = ExpTracker(), BarKills()
    k.update(0, 0.0, party=True)
    t = 0.0
    for i, v in enumerate(seq):
        t = float(i)
        e.add(Reading(t, LV, v, bar_percent(v, NEED)), now=t)
        if i == line_at:
            k.line(t - 0.3, 800)
            k.bonus(200, t - 0.2)
        k.shown(t)
        k.update(e.current_gain, t, party=True, hold=e.behind, unconfirmed=e.unconfirmed_gain,
                 bar_t=e.last_committed_t)
    for _ in range(10):
        t += 1
        k.update(e.current_gain, t, party=True, unconfirmed=e.unconfirmed_gain, bar_t=e.last_committed_t)
    return k.total


def test_h3_a_bar_read_a_little_low_then_true_is_one_step():
    x = 300_000
    assert bar_only([x] * 6 + [x + 970] + [x + 1000] * 8) == 1


def test_h4_a_spike_taken_back_is_no_step():
    x = 300_000
    assert bar_only([x] * 6 + [x + 1000] * 3 + [x + 1040] + [x + 1000] * 6) == 1


def test_two_spikes_in_a_row_are_no_step():
    x = 300_000  # the tracker verifies the first when the second comes; it falls back after
    assert bar_only([x] * 6 + [x + 1000] * 3 + [x + 1037, x + 1047] + [x + 1000] * 6) == 1


def drop_digit(v, rnd):
    s = str(v)
    if len(s) < 2:
        return v
    i = rnd.randrange(len(s))
    return int(s[:i] + s[i + 1:]) or v


def run(seed, gen, secs=60.0):
    """r4_probe's generators, shortened: a sparse party message area and bar."""
    rnd = random.Random(f"r4-{gen}-{seed}")
    mobs = [rnd.choice([300, 800, 1000, 2500]) for _ in range(rnd.randint(1, 3))]
    ratio = rnd.choice([0.05, 0.1, 0.2])
    fade, rows_max = rnd.uniform(7, 11), rnd.choice([2, 4, 6])
    gap = rnd.choice([(0.5, 2), (2, 6), (6, 14)])
    p_mis = rnd.choice([0, 0.02]) if gen == "base" else rnd.choice([0.02, 0.05, 0.1])
    p_row = rnd.choice([0, 0.05])
    p_fail = 0.03 if gen == "gapread" else 0.0
    p_low = 0.3 if gen == "barlow" else 0.0
    p_spike = 0.05 if gen == "barspike" else 0.0
    bar_dt = rnd.choice([0.2, 1.0])
    d, k, e = LineDiff(), BarKills(), ExpTracker()
    area, t, x, real, fail_until = [], 0.0, 200_000, 0, -1.0
    next_kill, next_bar, low = rnd.uniform(*gap), 0.0, None
    k.update(0, 0.0, party=True)

    def book(t):
        k.update(e.current_gain, t, party=True, hold=e.behind, unconfirmed=e.unconfirmed_gain,
                 bar_t=e.last_committed_t)
        assert k.total <= real, (gen, seed, t)

    while t < secs:
        t = round(t + 0.2, 3)
        if t >= next_kill and t < secs - 20:
            next_kill = t + rnd.uniform(*gap)
            s = rnd.choice(mobs)
            b = max(1, round(s * ratio))
            real += 1
            x += s + b
            area += [(K(s), t), (B(b), t)]
            if rnd.random() < p_low:
                low = x - rnd.randint(5, 60)
        area = [a for a in area if t - a[1] < fade][-rows_max:]
        if p_fail and t > fail_until and rnd.random() < p_fail:
            fail_until = t + 0.2 * rnd.randint(3, 6)
        frame = []
        if t > fail_until:
            n = len(area)
            for i, (txt, _) in enumerate(area):
                if rnd.random() < p_row:
                    continue
                if rnd.random() < p_mis:
                    v = int(txt.split("+")[1][:-1])
                    txt = txt.replace(f"+{v})", f"+{drop_digit(v, rnd)})")
                frame.append((txt, 100.0 - 12.0 * (n - 1 - i)))
        feed(d, k, frame, t)
        if t >= next_bar - 1e-9:
            next_bar = t + bar_dt
            v = x
            if low is not None:
                v, low = low, None
            elif rnd.random() < p_spike:
                v = x + rnd.randint(5, 60)
            e.add(Reading(t, LV, v, bar_percent(v, NEED)), now=t)
        book(t)
    for _ in range(60):
        t += 0.2
        book(t)
    return real, k.total


@pytest.mark.parametrize("gen", ["base", "mis1", "gapread", "barlow", "barspike"])
def test_party_count_never_passes_real_kills(gen):
    real = counted = 0
    for seed in range(60):  # 300 seeds over the five generators
        r, c = run(seed, gen)
        real, counted = real + r, counted + c
    assert counted <= real
