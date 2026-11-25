"""Fuzz: quickslot usage never overcounts by more than 2*K per segment.

Random true stack counts (uses, loot, restocks) are rendered as quickslot
reads with gaps, hover highlights, a stack dragged between slots and 0-20
misreads per hour, fed through the reader's confirm step into
``QuickslotUsage``. A segment is the stretch between restocks; the bound must
not grow with session length or with the number of misreads.

``python tests/test_usage_fuzz.py [seeds]`` prints the worst case over more seeds.
"""

import random

import numpy as np

from mctracker.items import K, QuickslotUsage, SlotAssignment, SlotItems
from mctracker.vision.quickslots import QuickslotReader

ICON = {"red": [10.0] * 12, "orange": [60.0] * 12, "hover": [35.0] * 12, "empty": [0.0] * 12}


def near(n: int) -> list[int]:
    """Numbers one digit edit (substitute, drop or add a digit) away from ``n``."""
    s, out = str(n), set()
    for i in range(len(s) + 1):
        out.update(s[:i] + d + s[i:] for d in "0123456789")
    for i in range(len(s)):
        out.add(s[:i] + s[i + 1:])
        out.update(s[:i] + d + s[i + 1:] for d in "0123456789")
    return sorted({int(x) for x in out if x} - {n})


def truth(rng, ticks, p_use, loot):
    """Per tick {item: true count}, plus uses and restocks per item."""
    cur = {"red": 300, "orange": 300}
    used = dict.fromkeys(cur, 0)
    restocks = dict.fromkeys(cur, 0)
    out = []
    for _ in range(ticks):
        for it in cur:
            if rng.random() < p_use and cur[it] > 3:
                n = rng.choice([1, 1, 1, 2, 3])
                cur[it] -= n
                used[it] += n
            if loot and rng.random() < 0.005:
                cur[it] += rng.randint(1, 3)
            if rng.random() < 0.0003:
                cur[it] += 100
                restocks[it] += 1
        out.append(dict(cur))
    return out, used, restocks


def run(seed, ticks=3600, two_slot=False, loot=True, p_use=0.01, mis_kind="any"):
    """(booked, used, restocks, misreads) per item for one random session."""
    rng = random.Random(seed)
    tr, used, restocks = truth(rng, ticks, p_use, loot)
    misreads_per_hour = rng.randint(0, 20)
    p_mis = misreads_per_hour / 3600 / (3 if two_slot else 2)
    reader = QuickslotReader(None)
    layout = {0: "red", 2: "orange", **({1: "red"} if two_slot else {})}
    si = SlotItems(None)
    for s, it in layout.items():
        si.slots[s] = SlotAssignment(s, ICON[it], None, it)
    q = QuickslotUsage()
    ev = dict.fromkeys(range(3))
    drag_until, nmis = -1, 0
    for t, true in enumerate(tr):
        if drag_until < t and rng.random() < 0.0005:
            drag_until = t + rng.randint(5, 300)  # red and orange swapped: both are gaps
        cur = dict(layout)
        if drag_until >= t:
            cur[0], cur[2] = "orange", "red"
        reader._n += 1
        reads = []
        for s in range(3):
            it = cur.get(s)
            if it is None:
                reads.append((None, ICON["empty"], 0))
                continue
            e = ev[s]
            if e is None or e[1] < t:
                ev[s] = None
                r = rng.random()
                if r < p_mis:
                    v = true[it]
                    if mis_kind == "up":
                        m = v + rng.randint(1, K)
                    elif mis_kind == "down":
                        m = max(0, v - rng.randint(1, K))
                    else:
                        m = rng.choice(near(v) + [v + rng.randint(1, K), max(0, v - rng.randint(1, K))])
                    ev[s] = ("mis", t + rng.randint(2, 90), m, v)
                    nmis += 1
                elif r < p_mis + 0.002:
                    ev[s] = ("gap", t + rng.randint(1, 60))
                elif r < p_mis + 0.004:
                    ev[s] = ("hover", t + rng.randint(1, 40))
            e, sig, raw = ev[s], ICON[it], true[it]
            if e:
                if e[0] == "mis":  # a misread sticks to the same pixels only
                    raw = e[2] if true[it] == e[3] else true[it]
                    if true[it] != e[3]:
                        ev[s] = None
                elif e[0] == "gap":
                    raw = None
                else:
                    sig, raw = ICON["hover"], None if rng.random() < 0.5 else true[it]
            st = reader._slots[s]
            if st.signature is None or np.abs(np.array(sig) - st.signature).mean() > 8:
                st.cache.clear()
                st.value = st.pending = None
            st.signature = np.array(sig)
            count, since = reader._confirm(st, raw)
            reads.append((count, sig, since))
        q.record(t, reads, si)
    q.finalise()
    booked = {it.key: n for it, n in q.final}
    return {k: (booked.get(k, 0), used[k], restocks[k]) for k in used}, nmis


def worst_overcount(seeds, ticks=3600, **kw):
    worst = {"over": -10**9, "over_per_segment": -10**9, "seed": None, "misreads": 0}
    for seed in seeds:
        res, nmis = run(seed, ticks, **kw)
        worst["misreads"] += nmis
        for got, used, restocks in res.values():
            over = got - used
            if over / (1 + restocks) > worst["over_per_segment"]:
                worst.update(over=over, over_per_segment=over / (1 + restocks), seed=seed)
    return worst


CONFIGS = {
    "one slot": {},
    "two slots": {"two_slot": True},
    "no loot": {"loot": False},  # loot nets against uses and would hide an overcount
    "no loot, up misreads": {"loot": False, "mis_kind": "up"},
    "no loot, down misreads": {"loot": False, "mis_kind": "down"},
    "no loot, two slots": {"loot": False, "two_slot": True},
    "idle, no loot": {"loot": False, "p_use": 0.001},
}


def test_overcount_is_bounded_per_segment():
    for name, kw in CONFIGS.items():
        w = worst_overcount(range(3), **kw)
        assert w["over_per_segment"] <= 2 * K, (name, w)


def test_overcount_does_not_grow_with_session_length():
    w = worst_overcount(range(1), ticks=6 * 3600, loot=False, p_use=0.002)
    assert w["over_per_segment"] <= 2 * K, w


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    for name, kw in CONFIGS.items():
        print(f"{name:24s}", worst_overcount(range(n), **kw))
    print(f"{'8 h idle, no loot':24s}", worst_overcount(range(max(1, n // 20)), ticks=8 * 3600, loot=False, p_use=0.001))
