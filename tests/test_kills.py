import random

from mctracker.kills import (
    KillCounter,
    LineTracker,
    Mob,
    estimate_missed,
    parse_exp_line,
)

MAP = [Mob(6, "Stump", 10), Mob(7, "Slime", 12), Mob(9, "Orange Mushroom", 15), Mob(42, "Boss-ish", 60)]


# --- parsing ---------------------------------------------------------------

def test_parse_exp_line():
    assert parse_exp_line("You have gained experience (+1,234)") == 1234
    assert parse_exp_line("You have gained experience (+15) (+3)") == 15
    assert parse_exp_line("You have acquired Snail Shell.") is None


# --- line tracking ---------------------------------------------------------

def test_lines_append():
    t = LineTracker()
    assert t.update(["a", "b"]) == ["a", "b"]
    assert t.update(["a", "b", "c"]) == ["c"]


def test_lines_scroll_off_top():
    t = LineTracker()
    t.update(["a", "b", "c"])
    assert t.update(["b", "c", "d", "e"]) == ["d", "e"]


def test_lines_unchanged():
    t = LineTracker()
    t.update(["a", "b"])
    assert t.update(["a", "b"]) == []


def test_lines_fade_without_new():
    t = LineTracker()
    t.update(["a", "b", "c"])
    assert t.update(["b", "c"]) == []


def test_duplicate_text_counts_while_growing():
    t = LineTracker()
    t.update(["+12"])
    assert t.update(["+12", "+12"]) == ["+12"]


def test_everything_replaced():
    t = LineTracker()
    t.update(["a", "b"])
    assert t.update(["x", "y"]) == ["x", "y"]


# --- kill classification -----------------------------------------------------

def simulate(scale, noise, n, seed=1):
    rng = random.Random(seed)
    truth, values = [], []
    for _ in range(n):
        mob = rng.choice(MAP)
        v = max(1, round(mob.exp * scale * rng.uniform(1 - noise, 1 + noise)))
        truth.append(mob.id)
        values.append(v)
    return truth, values


def accuracy(kc, truth, values):
    hits = sum((m.id if (m := kc.classify(v)) else None) == t for t, v in zip(truth, values))
    return hits / len(truth)


def test_solo_exact_values():
    kc = KillCounter()
    kc.set_map(MAP)
    for m in MAP:
        kc.add(m.exp)
    assert kc.counts() == {6: 1, 7: 1, 9: 1, 42: 1}


def test_party_share_scale_is_learned():
    # Party share ~ 35% of base EXP with small spread.
    truth, values = simulate(scale=0.35 * 20, noise=0.04, n=300)
    kc = KillCounter()
    kc.set_map(MAP)
    for v in values:
        kc.add(v)
    assert accuracy(kc, truth, values) > 0.9
    assert abs(kc.scale / (0.35 * 20) - 1) < 0.1


def test_exp_event_multiplier():
    truth, values = simulate(scale=2.0, noise=0.0, n=100)
    kc = KillCounter()
    kc.set_map(MAP)
    for v in values:
        kc.add(v)
    assert accuracy(kc, truth, values) == 1.0
    assert sum(kc.counts().values()) == 100


def test_outlier_is_unidentified():
    kc = KillCounter()
    kc.set_map(MAP)
    for _ in range(20):
        kc.add(12)
    kc.add(5000)
    c = kc.counts()
    assert c[7] == 20
    assert c[None] == 1


def test_new_map_resets_segment():
    kc = KillCounter()
    kc.set_map(MAP)
    kc.add(10)
    kc.set_map([Mob(1, "Snail", 2)])
    assert kc.total == 0


# --- reconciliation ----------------------------------------------------------

def test_estimate_missed():
    # Read 10 lines worth 100 total; the bar went up 150 -> ~5 missed.
    assert estimate_missed(100, 150, 10) == 5
    assert estimate_missed(100, 100, 10) == 0
