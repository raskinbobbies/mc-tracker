import random

from mctracker.kills import (
    KillCounter,
    LineTracker,
    Mob,
    estimate_missed,
    Message,
    parse_exp_line,
    parse_message,
)

MAP = [Mob(6, "Stump", 10), Mob(7, "Slime", 12), Mob(9, "Orange Mushroom", 15), Mob(42, "Boss-ish", 60)]


# --- parsing ---------------------------------------------------------------

def test_parse_real_messages():
    # Strings as OCR'd from real screenshots
    assert parse_exp_line("You received EXP (+82)") == 82
    assert parse_exp_line("You received EXP (+1,234)") == 1234
    assert parse_message("Bonus EXP for PARTY (+6)") == Message("party_bonus", 6)
    assert parse_exp_line("Bonus EXP for PARTY (+6)") is None
    assert parse_message("Echopus Leg x1 earned. (Etc)") == Message("loot", 1, "Echopus Leg", "Etc")
    assert parse_message("[18:08] raskinbobbi : thx") is None


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


def test_identical_lines_with_positions():
    # Two "+82" kills: second frame shows the first pushed up one slot.
    t = LineTracker(line_height=22)
    assert t.update([("You received EXP (+82)", 880)]) == ["You received EXP (+82)"]
    assert t.update([("You received EXP (+82)", 858), ("You received EXP (+82)", 880)]) == [
        "You received EXP (+82)"
    ]
    assert t.update([("You received EXP (+82)", 858), ("You received EXP (+82)", 880)]) == []


def test_ocr_drops_a_middle_line():
    t = LineTracker(line_height=22)
    t.update([("a1", 836), ("b2", 858), ("c3", 880)])
    assert t.update([("a1", 836), ("c3", 880)]) == []


def test_party_kills_unidentified_when_mobs_close():
    kc = KillCounter()
    kc.set_map([Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98), Mob(58, "Echopus", 107)])
    kc.add(82)
    kc.add(39, party=True)
    assert kc.counts() == {54: 1, None: 1}


def test_party_kills_identified_when_mobs_far_apart():
    kc = KillCounter()
    kc.set_map([Mob(1, "Small", 10), Mob(2, "Big", 100)])
    kc.add(10)
    kc.add(95, party=True)
    assert kc.counts() == {1: 1, 2: 1}


FOREST = [Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98), Mob(58, "Echopus", 107)]


def test_two_types_never_share_a_monster():
    # Live values can differ a bit from the reference data. Before any Echopus
    # is killed, mushrooms and lizards must still map to two different monsters.
    kc = KillCounter()
    kc.set_map(FOREST)
    for v in [88] * 6 + [101] * 11:
        kc.add(v)
    assert kc.counts() == {54: 6, 57: 11}


def test_exact_values_stay_exact_before_third_type():
    kc = KillCounter()
    kc.set_map(FOREST)
    for v in [82] * 6 + [98] * 11:
        kc.add(v)
    assert kc.counts() == {54: 6, 57: 11}
    kc.add(107)
    assert kc.counts() == {54: 6, 57: 11, 58: 1}


def test_single_type_prefers_unscaled_match():
    kc = KillCounter()
    kc.set_map(FOREST)
    for _ in range(6):
        kc.add(82)
    assert kc.counts() == {54: 6}
