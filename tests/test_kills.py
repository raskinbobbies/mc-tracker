import random

from mctracker.kills import (
    KillCounter,
    LineDiff,
    Mob,
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

def E(n):
    return f"You received EXP (+{n})"


a, b, c, d, e = (E(n) for n in (11, 22, 33, 44, 55))


class Diff(LineDiff):
    """LineDiff returning EXP lines as text, for short tests."""

    def update(self, lines):
        return [E(m.value) for m in super().update(lines)]


def test_lines_append():
    t = Diff()
    assert t.update([a, b]) == [a, b]
    assert t.update([a, b, c]) == [c]


def test_lines_scroll_off_top():
    t = Diff()
    t.update([a, b, c])
    assert t.update([b, c, d, e]) == [d, e]


def test_lines_unchanged():
    t = Diff()
    t.update([a, b])
    assert t.update([a, b]) == []


def test_lines_fade_without_new():
    t = Diff()
    t.update([a, b, c])
    assert t.update([b, c]) == []


def test_duplicate_text_counts_while_growing():
    t = Diff()
    t.update([E(12)])
    assert t.update([E(12), E(12)]) == [E(12)]


def test_everything_replaced():
    t = Diff()
    t.update([a, b])
    assert t.update([d, e]) == [d, e]


def test_noise_lines_are_ignored():
    # Damage numbers and fading fragments never count as messages.
    t = Diff()
    assert t.update(["219", a, "167"]) == [a]
    assert t.update(["1074", a, "ZZO"]) == []


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


def test_identical_lines_with_positions():
    # Two "+82" kills: second frame shows the first pushed up one slot.
    t = Diff()
    assert t.update([("You received EXP (+82)", 880)]) == ["You received EXP (+82)"]
    assert t.update([("You received EXP (+82)", 858), ("You received EXP (+82)", 880)]) == [
        "You received EXP (+82)"
    ]
    assert t.update([("You received EXP (+82)", 858), ("You received EXP (+82)", 880)]) == []


def test_ocr_drops_a_middle_line():
    t = Diff()
    t.update([("a1", 836), ("b2", 858), ("c3", 880)])
    assert t.update([("a1", 836), ("c3", 880)]) == []


def test_party_kills_unidentified_when_mobs_close():
    kc = KillCounter()
    kc.set_map([Mob(54, "Glowshroom", 82), Mob(57, "Aqumander", 98), Mob(58, "Echopus", 107)])
    for v in (98, 98, 107, 107):  # solo kills of adjacent mobs: a trusted solo scale
        kc.add(v)
    kc.add(39, party=True)
    # 107 < 98 * E_MAX: the top-mob window is empty, so no share counts.
    assert kc.counts() == {57: 2, 58: 2, None: 1}


def test_party_kills_only_count_for_the_top_mob_window():
    # Far apart or not, a party share is never classified mob by mob: only a share
    # worth more than the second mob could give counts, and only for the top mob.
    kc = KillCounter()
    kc.set_map([Mob(1, "Small", 10), Mob(2, "Big", 100)])
    for v in (10, 10, 100, 100):  # solo kills in 2 clusters: a trusted solo scale
        kc.add(v)
    for v in (95, 92, 98):
        kc.add(v, party=True)
    kc.add(9, party=True)  # could be Small's full share or a sliver of Big's: unassigned
    assert kc.counts() == {1: 2, 2: 5, None: 1}


def test_one_off_solo_line_in_a_party_is_not_trusted():
    # In a party a line flagged solo may be a share whose bonus line was missed: only a
    # value seen twice (a real solo kill gives the same number each time) is counted.
    kc = KillCounter()
    kc.set_map([Mob(1, "A", 300), Mob(2, "B", 700)])
    for v in (100, 300, 300, 700, 700):  # 100: a misread share of A, fits a 0.39x scale
        kc.add(v)
    for v in (250, 120, 290):  # below 300 * E_MAX: not in the top-mob window
        kc.add(v, party=True)
    assert kc.counts() == {1: 2, 2: 2, None: 4}


def test_party_share_is_not_classified_on_a_solo_scale():
    # One stray solo line would fit a 0.4x scale (40 = Big), put the window at 4.2..42
    # and count the Small shares below as Big. It is a one-off, so it is not trusted,
    # and under 3 solo lines the window never starts below 1x.
    kc = KillCounter()
    kc.set_map([Mob(1, "Small", 10), Mob(2, "Big", 100)])
    kc.add(40)  # a misread share of Big, flagged solo
    for v in (9, 8, 10, 7):
        kc.add(v, party=True)
    kc.add(60, party=True)
    c = kc.counts()
    assert c == {2: 1, None: 5}


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


def test_solo_kills_of_mobs_within_tolerance_stay_unassigned():
    # 130 and 135 are within cluster_tol: their solo kills merge into one cluster, so
    # neither is credited (was: the 135 mob got all 5, truth 3).
    kc = KillCounter()
    kc.set_map([Mob(1, "a", 54), Mob(2, "b", 63), Mob(3, "c", 130), Mob(4, "d", 135)])
    for v in (54, 54, 63, 63, 130, 130, 135, 135, 135):
        kc.add(v)
    kc.add(40, party=True)  # a party segment
    n = kc.counts()
    assert n[3] == 0 and n[4] == 0, n


def test_solo_close_mobs_unassigned_outside_a_party_too():
    kc = KillCounter()
    kc.set_map([Mob(1, "a", 54), Mob(3, "c", 130), Mob(4, "d", 135)])
    for v in (54, 54, 130, 130, 135, 135, 135):
        kc.add(v)
    n = kc.counts()
    assert n[1] == 2 and n[3] <= 2 and n[4] <= 3, n


def test_steady_ratio_missed_bonus_shares_do_not_skip_a_mob():
    # 400/1000/3000: shares of the 400 and 1000 mobs at a steady ~0.36-0.45 lost their
    # bonus lines (178, 939, each seen often). They fit 0.36x as 400 -> 178 and
    # 3000 -> 939, skipping the 1000 mob, so the 939s counted as the 3000 mob.
    kc = KillCounter()
    kc.set_map([Mob(1, "A", 400), Mob(2, "B", 1000), Mob(3, "C", 3000)])
    for _ in range(6):
        kc.add(939)
    for _ in range(3):
        kc.add(178)
    for _ in range(10):
        kc.add(450, party=True)
    n = kc.counts()
    assert n[3] == 0 and n[2] == 0 and n[1] <= 3, n
    assert not kc._solo_trusted


def test_a_line_above_the_top_mob_stops_party_counting():
    # A 3x event (above E_MAX): one line beyond the top mob's full value shows the
    # window can't be trusted, so no party share counts for the segment.
    kc = KillCounter()
    kc.set_map([Mob(1, "A", 300), Mob(2, "B", 700)])
    for v in (650, 680, 2000):
        kc.add(v, party=True)
    assert kc.counts() == {None: 3}
