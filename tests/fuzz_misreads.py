"""Seeded misread fuzz for ExpTracker (round-5 review, extends the reviewer's fz.py).

Each run grinds a true bar (low, mid or high level, still or moving, with the
odd death), then injects one misread of the level digits, EXP, percent or a
combination (l, e, p, le, lp, ep, lep), chosen so the misread reading fits some
level, 1-30 times: on a still bar (inserted repeats) or a moving one (replacing
frames). Used by tests/test_review_round5.py; ``python tests/fuzz_misreads.py N``
prints the measured undercount for N runs.
"""

from __future__ import annotations

import logging
import random
import sys
from dataclasses import dataclass

from mctracker.exp_table import EXP_TABLE as T
from mctracker.exp_table import bar_percent as bp
from mctracker.exp_tracker import ExpTracker, Reading

KINDS = ["l", "e", "p", "le", "lp", "ep", "lep"]
_FITTER = ExpTracker()


def _subs(s: str) -> list[str]:
    """Single-glyph misreads: a digit swapped, dropped or doubled."""
    out = []
    for i, c in enumerate(s):
        if c.isdigit():
            out += [s[:i] + d + s[i + 1 :] for d in "0123456789" if d != c]
            out += [s[:i] + s[i + 1 :], s[:i] + c + s[i:]]
    return [o for o in out if o and o[0] != "." and o.count(".") <= 1 and not o.endswith(".")]


def _misreads(level: int, exp: int, pct: float, kind: str, rnd: random.Random) -> list[tuple]:
    ls = [int(x) for x in _subs(str(level)) if x.isdigit() and 1 <= int(x) <= 300] if "l" in kind else [level]
    es = [int(x) for x in _subs(str(exp)) if x.isdigit()] if "e" in kind else [exp]
    ps = [float(x) for x in _subs(f"{pct:.2f}")] if "p" in kind else [pct]
    combos = [(lv, e, p) for lv in ls for e in es for p in ps]
    rnd.shuffle(combos)
    out = []
    for lv, e, p in combos[:3000]:
        f = _FITTER.fits(Reading(0, lv, e, p))
        if f and (f != [level] or e != exp):  # fits somewhere it shouldn't
            out.append((lv, e, p))
        elif "l" in kind and kind != "l" and f == [level] and lv != level:
            out.append((lv, e, p))
    if kind == "l":  # digits alone never change the fit; still worth feeding
        out = [(lv, exp, pct) for lv in ls]
    return out


def _truth(rnd: random.Random, n: int = 140):
    band = rnd.choice(["low", "mid", "high"])
    level = {"low": rnd.randint(2, 15), "mid": rnd.randint(20, 55), "high": rnd.randint(56, 85)}[band]
    exp = rnd.randint(0, T[level] - 1)
    kf = rnd.choice([0.03, 0.1, 0.3]) if band == "low" else rnd.choice([0.002, 0.005, 0.01, 0.03, 0.08])
    still = rnd.choice([0.0, 0.3, 0.6, 0.85])
    frames, gained, lost = [], 0, 0
    for _ in range(n):
        if rnd.random() > still:
            k = max(1, int(T[level] * kf * rnd.uniform(0.5, 1.5)))
            gained += k
            exp += k
            while exp >= T[level] and level < 89:
                exp -= T[level]
                level += 1
            if exp >= T[level]:
                gained -= exp - (T[level] - 1)
                exp = T[level] - 1
        if rnd.random() < 0.004 and level >= 10:
            pen = min(exp, T[level] // 10 if rnd.random() < 0.7 else T[level] // 100)
            exp -= pen
            lost += pen
        frames.append((level, exp, gained, lost))
    return band, frames


@dataclass
class Run:
    seed: int
    band: str
    kind: str
    moving: bool
    reps: int
    need: int
    true_gain: int
    true_levels: int
    true_net: int
    gained: int
    levels: int
    net: int
    longest_stall: int  # distinct true readings in a row without a commit, misreads inactive
    verified_monotonic: bool

    @property
    def undercount(self) -> int:
        return self.true_gain - self.gained


def run_one(seed: int) -> Run | None:
    rnd = random.Random(seed)
    band, frames = _truth(rnd)
    at = rnd.randint(20, len(frames) - 40)
    level, exp, _, _ = frames[at]
    kind = rnd.choice(KINDS)
    bad = _misreads(level, exp, bp(exp, T[level]), kind, rnd)
    if not bad:
        return None
    mis = rnd.choice(bad)
    reps = rnd.choice([1, 2, 3, 5, 10, 20, 30])
    moving = rnd.random() < 0.4
    feed = [(lv, e, bp(e, T[lv]), False) for lv, e, _, _ in frames]
    if moving:
        for j in range(at, min(len(feed), at + reps)):
            feed[j] = (*mis, True)
    else:
        feed = feed[: at + 1] + [(*mis, True)] * reps + feed[at + 1 :]
    t = ExpTracker()
    longest, seen, vs = 0, set(), []
    for i, (lv, e, p, is_bad) in enumerate(feed):
        before = t._commits
        t.add(Reading(i, lv, e, p))
        vs.append(t.verified_gain)
        if is_bad:
            seen = set()
        elif t._commits == before:
            seen.add(e)
            longest = max(longest, len(seen))
        else:
            seen = set()
    t.finalize()
    first, last = frames[0], frames[-1]
    return Run(
        seed, band, kind, moving, reps, T[level],
        true_gain=last[2] - first[2], true_levels=last[0] - first[0], true_net=last[2] - first[2] - (last[3] - first[3]),
        gained=t.gained, levels=t.levels_gained, net=t.net, longest_stall=longest, verified_monotonic=vs == sorted(vs),
    )


def run_many(n: int, seed0: int = 0) -> list[Run]:
    logging.disable(logging.WARNING)
    try:
        return [r for s in range(seed0, seed0 + n) if (r := run_one(s)) is not None]
    finally:
        logging.disable(logging.NOTSET)


def report(runs: list[Run]) -> str:
    lines = [f"{len(runs)} runs"]
    for band in ("low", "mid", "high", None):
        rs = [r for r in runs if band in (None, r.band)]
        if not rs:
            continue
        tg = sum(r.true_gain for r in rs)
        under = sum(r.undercount for r in rs)
        worst = max(r.undercount / r.need for r in rs)
        lv = sum(r.true_levels - r.levels for r in rs)
        lines.append(
            f"{band or 'all':>4}: n={len(rs)} undercount {100 * under / max(1, tg):.2f}% of true gain, "
            f"worst run {100 * worst:.1f}% of a level, levels missed {lv}, "
            f"exact {sum(r.gained == r.true_gain for r in rs)}, max stall {max(r.longest_stall for r in rs)}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    runs = run_many(n, int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    print(report(runs))
    for r in runs:
        if r.levels > r.true_levels or r.gained > r.true_gain + r.need // 100 or r.longest_stall > 10:
            print("VIOLATION", r)
