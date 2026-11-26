import json
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from mctracker import history
from mctracker.items import CONFIRM, K, S, AppSettings, ItemSeries, PriceList, QuickslotUsage, SlotItems, spent
from mctracker.kills import Message, Mob, parse_message
from mctracker.session import Session, Tracker
from mctracker.vision import quickslots as qs
from mctracker.vision.quickslots import digit_mask, parse_count

HUNT = [Mob(54, "Glowshroom", 82)]
RED = [200.0] * 24  # icon fingerprints
BLUE = [40.0] * 24
EMPTY = [90.0] * 24  # empty slot background


# --- parsing ---------------------------------------------------------------------

def test_parse_mesos_messages():
    assert parse_message("You have gained mesos (+10)") == Message("mesos", 10)
    assert parse_message("You have gained mesos (+1,234)") == Message("mesos", 1234)
    assert parse_message("Youhavegainedmesos(+56)") == Message("mesos", 56)
    assert parse_message("You have gained mesos ( + 1, 050 )") == Message("mesos", 1050)
    assert parse_message("You have gained meso5 (+7)") == Message("mesos", 7)
    assert parse_message("You have gained mesos (+0)") is None
    assert parse_message("You received EXP (+82)") == Message("exp", 82)


def test_parse_slot_count():
    assert parse_count("228") == 228
    assert parse_count("1:3") == 13
    assert parse_count("1O4") == 104
    assert parse_count("") is None
    assert parse_count("x9") is None
    assert parse_count("12345") is None


def test_digit_mask_keeps_only_outlined_fill():
    img = np.full((32, 66, 3), 240, np.uint8)  # light slot background
    img[6:28, 4:12] = 0  # black outline box ...
    img[8:26, 6:10] = 255  # ... around a white glyph stroke
    img[2:20, 30:40] = 255  # white icon area, not enclosed in black
    m = digit_mask(img)
    assert m[8:26, 6:10].all()
    assert not m[:, 20:].any()




# --- items used: derived from a per-session series --------------------------------------
#
def assigned(*slots):
    """SlotItems with ``(slot, icon, item id, name)`` assignments, not saved."""
    si = SlotItems()
    for slot, sig, item_id, name in slots:
        si.assign(slot, sig, item_id, name)
    return si


RED_SLOT = (0, RED, 2000000, "Red Potion")
ORANGE_SLOT = (1, BLUE, 2000001, "Orange Potion")
HOVER = [215.0] * 24  # RED with a hover highlight: further than SAME_ICON from the assigned icon


def usage(si, ticks):
    """QuickslotUsage fed one tick per entry of ``ticks`` (a list of ``(count, icon)`` per slot)."""
    u = QuickslotUsage()
    for t, slots in enumerate(ticks):
        u.record(t, [(c, sig, 0) for c, sig in slots], si)
    return u


def booked(u):
    return {it.name: n for it, n in u.uses()}


# Notation in the tables: one value per tick (quickslot read); None is a gap; a
# string is a misread. Values are what reaches the series, i.e. already
# confirmed by two identical reads (that step is tested on its own below).
# Uses are net change between stable values (held >= S reads, or a descending
# chain of >= S readings): see the notes in items.py.

M = str  # marks a misread in the tables


def uses(values):
    """Uses for one item read once per tick."""
    it = ItemSeries("red", "Red")
    for t, v in enumerate(values):
        if v is not None:
            it.add(t, int(v))
    return it.uses()


def held(v, n=S):
    return [v] * n


def each_held(*vals):
    return [x for v in vals for x in held(v)]


def rep(base, event, period=180, ticks=3600):
    """``event`` every ``period`` ticks on a still ``base``, for an hour."""
    out = []
    while len(out) < ticks:
        out += [*event, *held(base, period - len(event))]
    return out


# The earlier regression table. Every row as written shows each true value for
# one or two reads, which is never stable now, so those rows give 0: an
# undercount, never more than the truth. Each row is also run with every true
# value held for S reads, where it gives the old expected value.
# Columns: as written, got (was), held, got, true uses.
TABLE = [
    ("18-13-17-16", [18, 18, 13, 13, 17, 17, 16, 16], {0},  # was {2}
     each_held(18, M(13), 17, 16), {2}, 2),
    ("83-88-82", [83, *held(M(88), 70), 82], {0},  # was {1}
     [*held(83), *held(M(88), 70), *held(82)], {1}, 1),
    ("18-13x70-17", [18, *held(M(13), 70), 17, 16], {0},  # was {2}
     [*held(18), *held(M(13), 70), *held(17), *held(16)], {2}, 2),
    ("18-13x70-16", [18, *held(M(13), 70), 16, 15], {0},  # was {3}
     [*held(18), *held(M(13), 70), *held(16), *held(15)], {3}, 3),
    ("25-2-24", [25, *held(M(2), 30), 24], {0},  # was {0, 1}
     [*held(25), *held(M(2), 30), *held(24)], {1}, 1),
    ("loot", [50, 49, 48, 49, 48, 47], {0},  # was {3}
     each_held(50, 49, 48, 49, 48, 47), {3}, 4),
    ("tooltip", [3, *held(None, 6), 3, 3, 2], {0},  # was {1}
     [*held(3), *held(None, 6), *held(3), *held(2)], {1}, 1),
    ("120-12-119", [120, M(12), 119], {0},  # was {0, 1}
     [*held(120), M(12), *held(119)], {1}, 1),
    ("100-to-60", [*range(100, 59, -1), *held(60, 10)], {40},  # unchanged: a descending chain is stable
     [*range(100, 59, -1), *held(60, 10)], {40}, 40),
    ("restock", [*range(50, 40, -1), 140, *range(139, 129, -1)], {19},  # unchanged
     [*range(50, 40, -1), 140, *range(139, 129, -1)], {19}, 19),
]


@pytest.mark.parametrize("written, got_written, held_values, got_held, true",
                         [r[1:] for r in TABLE], ids=[r[0] for r in TABLE])
def test_usage_table(written, got_written, held_values, got_held, true):
    assert uses(written) in got_written and uses(written) <= true
    assert uses(held_values) in got_held and uses(held_values) <= true


def test_usage_never_overcounts_misreads():
    assert uses([25, *held(M(2), 30)]) == 0  # misread to the end: 25 never stable
    assert uses([*held(25), *held(M(2), 30)]) == 0  # unlinked endpoint: falls back to 25


def test_sell_then_partial_rebuy():
    # 120 -> 12 (sold) -> 100 (bought back) looks exactly like 120, "12", 100.
    assert uses([*held(120), *held(12, 10), *held(100)]) == 0
    assert uses([*held(120), *held(12, 10), *held(100), *held(99)]) == 1  # only the use after the rebuy


def test_real_big_drops_and_restocks():
    assert uses(each_held(200, 100, 99)) == 1  # 200 -> 100 is unlinked: the endpoint falls back
    assert uses(each_held(10, 5, 100, 99)) == 1  # was 1
    assert uses([*held(10), 9, 8, 7, 6, 5, *held(100), 99]) == 5  # chain 10 -> 5; 99 read once
    assert uses(each_held(10, None, 8)) == 2  # a step of at most K is trusted across a gap
    # Changed: was 1 + 3 + 1 (K per observed change). 120 -> 80 across a gap is
    # in the middle of the segment, so it cancels like anything else: the net
    # 42, which is the truth.
    # Changed again: a drop of more than R across a gap counts once the lower level has
    # shown for CONFIRM ticks (sticky misreads look the same until then).
    assert uses([*held(121), *held(120), *held(None, 40), *held(80), *held(79)]) == 2
    assert uses([*held(121), *held(120), *held(None, 40), *held(80), *held(79, CONFIRM)]) == 42
    assert uses([*held(121), *held(120), *held(None, 40), *held(80)]) == 1  # unlinked endpoint: falls back
    assert uses([*held(200), *held(20, 65), *held(19), *held(18)]) == 2


def test_misread_after_a_gap_is_bounded():
    assert uses([*held(120), *held(None, 40), *held(M(12)), *held(117), *held(116)]) == 4
    assert uses([*held(20), *held(None, 6), *held(M(2)), *held(19), *held(18)]) == 2
    assert uses([*held(120), *held(None, 40), *held(M(12))]) == 0  # misread to the end: falls back
    assert uses([*held(M(81), 20), *held(30), *held(29)]) == 1  # misread first value: falls back
    assert uses([*held(21), *held(20), *held(M(7)), *held(19)]) == 2


def test_brief_values_never_count():
    assert uses([18, 18, 13, 13]) == 0
    assert uses([*held(100), 97, 97, *held(100)]) == 0
    assert uses([*held(50), 49, 48, 47]) == 0  # fewer than S readings: no stable end yet
    assert uses([*held(50), *held(47, S - 1)]) == 0
    assert uses([*held(50), *held(47)]) == 3  # now 47 is stable


def test_live_display_uses_the_latest_stable_value():
    it = ItemSeries("red", "Red")
    for t, v in enumerate([*held(50), *held(48)]):
        it.add(t, v)
    assert it.uses() == 2
    for t, v in enumerate(held(M(4)), start=10):  # a misread becomes stable: falls back
        it.add(t, int(v))
    assert it.uses() == 2
    for t, v in enumerate(held(47), start=15):
        it.add(t, v)
    assert it.uses() == 3


def test_dips_and_spikes_are_misreads():
    assert uses(each_held(100, M(10), 100)) == 0
    assert uses(each_held(100, M(10), 97)) == 3  # used during the misread
    assert uses(each_held(18, M(180), 18, 17)) == 1  # not a restock
    assert uses(each_held(100, M(1000), 99)) == 1
    # a real small restock (within R) after a big drop looks like a dip: undercount
    assert uses(each_held(15, 5, 20, 19)) <= 11


# --- review round 5: misreads booked per occurrence ------------------------------------

@pytest.mark.parametrize("values", [
    rep(16, [M(18)] * 2),  # still 16 reading "18" for 2 reads every 3 min
    rep(100, [M(103)] * 2),  # still 100 reading "103"
    rep(100, [M(98)] * 40),  # 100, "98" x40, 100, repeated
    rep(100, [M(98)] * 2),
    rep(100, [M(99)] * 2 + [M(98)] * 2),  # two-step misreads
    rep(100, [M(98)] * 2 + [M(99)] * 2),
    rep(100, [M(99)] * 6 + [M(98)] * 6),  # two-step, both stable
    rep(100, [M(97)] * 2 + [None] * 35),
    rep(100, [M(10)] * 2),
    rep(100, [M(10)] * 6),  # "10" for a still 100, stable each time
    rep(100, [M(0)] * 2),
], ids=["16-18", "100-103", "100-98x40", "100-98", "99-98", "98-99", "99-98-stable", "97-gap",
        "10", "10-stable", "0"])
def test_still_stack_misreads_book_nothing(values):
    assert uses(values) == 0
    assert booked(usage(assigned(RED_SLOT), [[(c if c is None else int(c), RED)] for c in values])) == {}


def test_two_slot_alternating_hovers_book_nothing():
    # Slot 2 misreads a still 100 as "98" whenever slot 1 is hovered, and the
    # hover alternates every 40 ticks for an hour.
    si = assigned(RED_SLOT, (1, RED, 2000000, "Red Potion"))
    ticks = [[(100, RED), (100, RED)]] * 5
    for _ in range(45):
        ticks += [[(None, HOVER), (98, RED)]] * 40 + [[(100, RED), (None, HOVER)]] * 40
    assert booked(usage(si, ticks)) == {}


def test_moved_stack_books_the_uses_after_reassigning():
    # Red is used once in slot 1, dragged to slot 4 and used 10 times while
    # untracked, then assigned there (slot 1 now shows nothing) and used 20 times.
    si = assigned(RED_SLOT)
    u, t, v = QuickslotUsage(), 0, 100

    def tick(*slots):
        nonlocal t
        u.record(t, [(c, sig, 0) for c, sig in slots], si)
        t += 1
    empty = (None, EMPTY)
    for _ in range(5):
        tick((v, RED), empty, empty, empty)
    v -= 1
    for _ in range(5):
        tick((v, RED), empty, empty, empty)
    for _ in range(10):
        v -= 1
        tick(empty, empty, empty, (v, RED))
    si.assign(3, RED, 2000000, "Red Potion", shown=[EMPTY, EMPTY, EMPTY, RED])
    assert set(si.slots) == {3}  # slot 1 doesn't show red any more: dropped
    for _ in range(5):
        tick(empty, empty, empty, (v, RED))
    for _ in range(20):
        v -= 1
        tick(empty, empty, empty, (v, RED))
    for _ in range(5):
        tick(empty, empty, empty, (v, RED))
    assert 21 <= booked(u)["Red Potion"] <= 31  # 1 + 10 untracked + 20; at least the 20 after


def test_assign_keeps_other_slots_that_still_show_the_item():
    si = assigned(RED_SLOT)
    si.assign(1, RED, 2000000, "Red Potion", shown=[RED, RED])
    assert set(si.slots) == {0, 1}
    si.assign(2, BLUE, 2000001, "Orange Potion", shown=[EMPTY, RED, BLUE])
    assert set(si.slots) == {0, 1, 2}  # other items are left alone


def test_slot_items_old_format_is_updated_once(tmp_path):
    p = tmp_path / "slot_items.json"
    p.write_text(json.dumps([{"signature": RED, "id": 2000000, "name": "Red Potion"}]), encoding="utf-8")
    si = SlotItems(p)
    assert si.slots == {} and "format" in si.problem and "bad" not in si.problem and "assign your items again" in si.problem
    assert json.loads(p.read_text(encoding="utf-8")) == []  # saved once
    again = SlotItems(p)
    assert again.problem is None and again.slots == {}
    assert not list(tmp_path.glob("*.bad-*"))


def ticks_held(*per_tick):
    """Each per-tick slot list held for S ticks."""
    return [t for x in per_tick for t in [x] * S]


def test_slot_emptying_books_nothing():
    # We can't tell an empty slot from a moved stack, so the last few go uncounted.
    u = usage(assigned(RED_SLOT), ticks_held([(3, RED)], [(None, EMPTY)]))
    assert u.uses() == []


def test_unassigned_slots_are_not_tracked():
    u = usage(assigned(), ticks_held(*([(c, RED), (c, BLUE)] for c in [10, 9, 8])))
    assert u.uses() == [] and u.items == {}
    u = usage(assigned(RED_SLOT), ticks_held(*([(c, RED), (c, BLUE)] for c in [10, 9, 8])))
    assert booked(u) == {"Red Potion": 2}  # slot 2 (orange) isn't assigned


def test_two_assigned_items_with_flicker_book_only_real_uses():
    # Review of 9c54203: red and orange both at 100 in two slots; red used 3
    # times, then for an hour one slot reads None for a tick once a minute.
    si = assigned(RED_SLOT, ORANGE_SLOT)
    ticks, red = [], 100
    for t in range(3700):
        if t in (10, 20, 30):
            red -= 1
        slots = [(red, RED), (100, BLUE)]
        if t > 40 and t % 60 == 0:
            slots[(t // 60) % 2] = (None, slots[(t // 60) % 2][1])
        ticks.append(slots)
    assert booked(usage(si, ticks)) == {"Red Potion": 3}


def test_other_item_dragged_onto_a_slot_is_not_counted_until_reassigned():
    si = assigned(RED_SLOT)
    u = QuickslotUsage()
    t = 0
    for c, sig in [(50, RED)] * S + [(c, BLUE) for c in range(100, 90, -1) for _ in range(S)]:
        u.record(t, [(c, sig, 0)], si)  # orange dragged onto red's slot, then used
        t += 1
    assert u.uses() == [] and list(u.items) == ["redpotion"]
    si.assign(0, BLUE, 2000001, "Orange Potion")  # the user reassigns the slot
    for c in [91] * S + [90] * S + [89] * S:
        u.record(t, [(c, BLUE, 0)], si)
        t += 1
    assert booked(u) == {"Orange Potion": 2}


def test_still_stack_flickering_down_and_back_books_nothing():
    # A cursor resting on a still 100 makes it read "98" for 2 reads every 3
    # minutes, for an hour.
    values = [98 if t % 180 in (100, 101) else 100 for t in range(3600)]
    assert booked(usage(assigned(RED_SLOT), [[(c, RED)] for c in values])) == {}
    assert uses(values) == 0


def test_hover_highlight_is_a_gap():
    # Review of 9c54203: the highlighted icon shows 3 of every 20 ticks while
    # using 1 per 10 ticks, 100 -> 40.
    ticks = [[(max(40, 100 - t // 10), HOVER if t % 20 in (10, 11, 12) else RED)] for t in range(640)]
    u = usage(assigned(RED_SLOT), ticks)
    assert booked(u) == {"Red Potion": 60} and len(u.items) == 1


def test_hover_with_unreadable_count_makes_no_second_series():
    ticks = [*ticks_held([(50, RED)]), *[[(None, HOVER)]] * 8, *ticks_held([(49, RED)], [(48, RED)])]
    u = usage(assigned(RED_SLOT), ticks)
    assert booked(u) == {"Red Potion": 2} and len(u.items) == 1


def test_same_item_in_two_slots_counts_once():
    si = assigned(RED_SLOT, (1, RED, 2000000, "Red Potion"))
    u = usage(si, ticks_held(*([(c, RED), (c, RED)] for c in [50, 49, 48])))
    assert booked(u) == {"Red Potion": 2} and len(u.items) == 1
    # slots that disagree: the value held longer wins, so a misread in one
    # slot of a still stack books nothing
    u = usage(si, [[(50, RED), (50, RED)]] * 20 + [[(50, RED), (47, RED)]] * 10)
    assert u.uses() == []
    # one slot alone can't move the item more than K per tick from the other's last value
    u = usage(si, [[(50, RED), (50, RED)]] * 5 + [[(None, HOVER), (5, RED)]] * 10)
    assert u.uses() == []  # 45 more than 50 in 5 ticks


def test_lone_slot_of_a_two_slot_item_is_bounded_by_the_other():
    si = assigned(RED_SLOT, (1, RED, 2000000, "Red Potion"))
    u = usage(si, [[(50, RED), (None, EMPTY)]] * 2)
    assert u.uses() == []  # never stable
    # slot 2 is covered at 50: slot 1 alone moves the item at most K per tick since
    u = usage(si, ticks_held([(50, RED), (50, RED)], [(49, RED), (None, HOVER)], [(1, RED), (None, HOVER)],
                             [(48, RED), (None, HOVER)]))
    assert booked(u) == {"Red Potion": 2}  # the lone "1" (49 below 50 within 6 ticks) is skipped
    for t in range(20, 25):
        u.record(t, [(47, RED, 0), (47, RED, 0)], si)
    assert booked(u) == {"Red Potion": 3}  # both slots agree on 47


def test_moved_stack_is_untracked_until_reassigned():
    si = assigned(RED_SLOT)
    u = usage(si, ticks_held([(20, RED), (None, EMPTY)], [(None, EMPTY), (20, RED)], [(None, EMPTY), (19, RED)]))
    assert u.uses() == []
    si.assign(1, RED, 2000000, "Red Potion", shown=[EMPTY, RED])  # slot 1 is dropped: it shows nothing
    assert set(si.slots) == {1}
    for t, c in enumerate([19] * S + [18] * S, start=15):
        u.record(t, [(None, EMPTY, 0), (c, RED, 0)], si)
    # same item, so the same series: 20 (slot 1) -> 19 (slot 2) -> 18
    assert booked(u) == {"Red Potion": 2}


def test_loot_nets_against_uses():
    # Changed: an exact return to the earlier value used to cancel only the
    # last down-step (100, 98, 97, 100 -> 3; 100, 98 x31, 100 -> 2). Now every
    # gain within R nets: what's booked is the net drop.
    assert uses(each_held(100, 98, 100)) == 0
    assert uses(each_held(100, 99, 98, 99)) == 1
    assert uses(each_held(100, 98, 97, 100)) == 0
    assert uses(each_held(100, 98, 99)) == 1
    assert uses([*held(100), *held(98, 31), *held(100)]) == 0
    # Changed: was 5. 99 -> 90 is 9 uses in one tick, faster than K: never uses, so it splits.
    assert uses(each_held(100, 99, 90, 95)) == 1


def test_confirmed_values_are_backdated():
    # A count confirmed one read after it first showed starts a tick earlier,
    # so steady steps stay linked (gap-free): 100 -> 98 -> 96 is trusted.
    def series(*adds):
        it = ItemSeries("red", "Red")
        for a in adds:
            it.add(*a)
        return it
    head = [(t, 100) for t in range(5)]
    it = series(*head, (6, 98, 1), (8, 96, 1), *[(t, 96) for t in range(9, 13)])
    assert it.segments[0].lanes[0][1][:2] == [5, 6] and it.uses() == 4
    it = series(*head, (6, 98), (8, 96), *[(t, 96) for t in range(9, 13)])  # not backdated: gaps
    assert it.uses() == 0  # 100 -> 96 isn't linked: the last value falls back on 100


def test_usage_is_cached_by_series_version():
    it = ItemSeries("red", "Red")
    for t, v in enumerate([*held(10), *held(9)]):
        it.add(t, v)
    assert it.uses() == 1 and len(it.segments[0].lanes[0]) == 2  # run-length encoded
    it.segments[0].lanes[0][1][2] = 5  # tamper: same version, cached result
    assert it.uses() == 1
    it.add(10, 5)  # extends the (tampered) run, already stable: no new run, still cached
    assert it.uses() == 1
    it.add(11, 4)  # a new run: recomputed, tampered run included (10 -> 5, unlinked: falls back)
    assert it.uses() == 0


def test_finished_segments_are_cached():
    it = ItemSeries("red", "Red")
    for t, v in enumerate([*held(10), *held(9)]):
        it.add(t, v)
    it.new_segment()
    for t, v in enumerate([*held(20), *held(18)], start=20):
        it.add(t, v)
    assert it.uses() == 3 and it.segments[0].done == 1 and it.segments[1].done is None
    it.segments[0].lanes[0][1][2] = 5  # tamper with the finished segment: its result stays
    it.add(40, 18)
    it.add(41, 17)
    assert it.uses() == 3


# --- the OCR source ------------------------------------------------------------------

def test_reader_confirms_counts_and_drops_cache_on_icon_change(monkeypatch):
    calls = []

    class FakeOcr:
        text = "18"

        def __call__(self, img, **kw):
            calls.append(self.text)
            return SimpleNamespace(txts=[self.text], scores=[0.99])

    sig = {"v": np.asarray(RED, np.float32)}
    mask = {"v": np.zeros((4, 4), np.uint8)}
    monkeypatch.setattr(qs, "panel", lambda f: np.zeros((qs.PANEL_PX[1], qs.PANEL_PX[0], 3), np.uint8))
    monkeypatch.setattr(qs, "digit_mask", lambda c: mask["v"])
    monkeypatch.setattr(qs, "icon_signature", lambda p, s: sig["v"])
    monkeypatch.setattr(qs, "render_digits", lambda m: np.zeros((2, 2, 3), np.uint8))
    ocr = FakeOcr()
    r = qs.QuickslotReader(ocr)
    mask["v"] = np.ones((4, 4), np.uint8)

    def counts():
        return r.read(None)[0]

    a, b = counts(), counts()
    assert (a.count, a.raw) == (None, 18) and (b.count, b.since) == (18, 1)
    assert len(calls) == 8  # one OCR per slot; then cached
    ocr.text = "13"
    assert counts().count == 18 and len(calls) == 8  # same pixels: cached value
    sig["v"] = np.asarray(BLUE, np.float32)  # icon changed: cache dropped, count re-confirmed
    c, d = counts(), counts()
    assert len(calls) == 16 and c.count is None and d.count == 13


def test_single_misread_never_reaches_the_series():
    r = qs.QuickslotReader(None)
    out = []
    for raw in [100, 100, 10, 100, 99, 99, None, 98, 98]:
        r._n += 1
        out.append(r._confirm(r._slots[0], raw))
    assert [c for c, _ in out] == [None, 100, None, 100, None, 99, None, None, 98]


def test_steady_potting_through_the_reader():
    r, it = qs.QuickslotReader(None), ItemSeries(np.asarray(RED, np.float32), 0)
    raws = [100, 100, *range(99, 59, -1), 60, 60]  # each value read once while potting
    for t, raw in enumerate(raws):
        r._n += 1
        c, since = r._confirm(r._slots[0], raw)
        if c is not None:
            it.add(t, c, since)
    # each value is confirmed by its neighbours (100 > 99 > 98), so every step
    # reaches the series as a descending chain, which is stable
    assert len(it.segments[0]) == 41 and it.uses() == 40


def test_neighbour_confirmation_needs_plausible_steps():
    def confirmed(raws):
        r = qs.QuickslotReader(None)
        out = []
        for raw in raws:
            r._n += 1
            out.append(r._confirm(r._slots[0], raw)[0])
        return [c for c in out if c is not None]
    assert confirmed([18, 18, 17, 18, 18]) == [18, 18, 18]  # flicker: 17 never confirmed
    assert confirmed([18, 18, 13, 12, 12]) == [18, 12]  # 18 -> 13 is too big a step
    assert confirmed([18, 18, 17, 16, 16]) == [18, 17, 16]


# --- prices, identity, spent/net -------------------------------------------------------

def test_price_list_roundtrip_and_spent(tmp_path):
    p = tmp_path / "prices.json"
    pl = PriceList(p)
    pl.set("2000000", "Red Potion", 50)
    pl.set("Lemon", "Lemon", 120)
    pl.save()
    pl2 = PriceList(p)
    assert pl2.price("2000000") == 50
    assert spent({"2000000": 10, "Lemon": 2, "2000001": 3}, pl2) == (740, 3)
    p.write_text(json.dumps({"2000001": 160}))  # plain id -> price form
    assert PriceList(p).price("2000001") == 160


def test_slot_items_roundtrip(tmp_path):
    si = SlotItems(tmp_path / "slots.json")
    si.assign(0, RED, 2000000, "Red Potion")
    si.assign(3, BLUE, None, "Orange Potion")
    again = SlotItems(tmp_path / "slots.json")
    a = again.get(0)
    assert (a.item_id, a.name, a.key) == (2000000, "Red Potion", "redpotion")
    assert a.matches([203.0] * 24) and not a.matches(BLUE) and not a.matches(None)
    assert again.get(3).item_id is None and again.get(1) is None
    again.assign(3, None, None, "")  # unassign
    assert set(SlotItems(tmp_path / "slots.json").slots) == {0}


def test_session_net_mesos():
    pl = PriceList()
    pl.set("2000000", "Red Potion", 50)
    s = Session(mobs=tuple(HUNT), prices=pl)
    s.on_message(Message("mesos", 1000))
    s.on_use(2000000, "Red Potion", 4)
    s.on_use_other("Slot 2 (D)", 3)  # unpriced
    assert (s.mesos, s.mesos_spent, s.net_mesos, s.unpriced_used) == (1000, 200, 800, 3)
    s.active_s = 1800
    assert s.net_mesos_per_hour() == 1600
    d = s.to_dict()
    assert (d["mesos"], d["mesos_spent"], d["net_mesos"]) == (1000, 200, 800)
    assert {e["name"]: e["count"] for e in d["used_list"]} == {"Red Potion": 4, "Slot 2 (D)": 3}


def reads(*counts, icon=RED):
    return [SimpleNamespace(count=c, signature=icon) for c in counts]


def start_session(tr):
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([("You received EXP (+82)", 0)], now=1)


def test_tracker_records_quickslot_use_and_saves(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.prices.set("2000000", "Red Potion", 50)
    tr.slot_items.assign(0, RED, 2000000, "Red Potion")
    tr.on_map("Ant Tunnel", HUNT)
    for c in [100, 100, 98, 98]:  # no session yet: not recorded
        tr.on_quickslots(reads(c))
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([("You received EXP (+82)", 0)], now=1)
    for now in (2, 2.15):  # a mesos line counts once read twice (a one-off read may be a misread)
        tr.on_message_lines([("You received EXP (+82)", -22), ("You have gained mesos (+300)", 0)], now=now)
    for c in [95] * 5 + [94] * 5 + [92] * 5:  # a fresh baseline at 95
        tr.on_quickslots(reads(c))
    s = tr.session
    assert s.used == {2000000: 3} and s.mesos == 300 and s.net_mesos == 150
    assert tr.today()["net_mesos"] == 150
    tr.on_map("Town", [])
    assert s.quickslots.final is not None  # frozen at session end
    saved = history.load(tmp_path)[0]
    assert (saved.mesos, saved.mesos_spent, saved.net_mesos) == (300, 150, 150)
    assert {e["name"]: e["count"] for e in saved.data["used_list"]} == {"Red Potion": 3}
    day = history.by_day([saved])[0]
    assert day.net_mesos == 150
    assert tr.today()["mesos"] == 300 and tr.today()["mesos_spent"] == 150


def test_slot_assigned_mid_session_is_tracked_from_then_on(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    start_session(tr)

    def tick(c):
        tr.on_quickslots([SimpleNamespace(count=None, signature=EMPTY), SimpleNamespace(count=c, signature=BLUE)])
    for c in [10, 9, 8]:  # not assigned: not tracked
        tick(c)
    assert not tr.session.used and not tr.session.used_other
    tr.slot_items.assign(1, BLUE, None, "Orange Potion")  # the dialog assigns it, no id yet
    for c in [8] * 5 + [7] * 5 + [6] * 5:
        tick(c)
    assert tr.session.used_other == {"Orange Potion": 2} and not tr.session.used
    tr.slot_items.get(1).item_id = 2000001  # the id lookup finished
    tick(6)
    assert tr.session.used == {2000001: 2} and not tr.session.used_other


def test_pause_starts_a_new_segment(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.slot_items.assign(0, RED, None, "Red Potion")
    start_session(tr)
    for c in range(30, 19, -1):  # 30 -> 20: real use
        tr.on_quickslots(reads(c))
    tr.pause()
    for c in [20, 18, 13, 13]:  # not recorded, including the misread
        tr.on_quickslots(reads(c))
    tr.resume()
    for c in [17, 17]:
        tr.on_quickslots(reads(c))
    assert tr.session.used_other == {"Red Potion": 10}  # nothing subtracted, nothing diffed across


def test_values_never_stable_book_nothing_at_session_end(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.slot_items.assign(0, RED, None, "Red Potion")
    start_session(tr)
    for c in [18, 18, 13, 13]:
        tr.on_quickslots(reads(c))
    a = tr.session
    tr.on_map("Other map", HUNT)  # session A ends with fewer than S reads of any value
    assert a.quickslots.final == [] and not a.used_other
    tr.on_status(45, 1100, 11.0, now=10)
    tr.on_message_lines([("You received EXP (+90)", 0)], now=11)
    for c in [17, 17]:
        tr.on_quickslots(reads(c))
    b = tr.session
    assert b is not a and not b.used_other and not a.used_other


def test_session_use_never_goes_negative():
    s = Session()
    s.on_use(2000000, "Red Potion", 2)
    s.on_use(2000000, "Red Potion", -5)
    s.on_use_other("Slot 1 (Shift)", -1)
    assert not s.used and not s.used_other and s.mesos_spent == 0


# --- settings files and price matching ----------------------------------------------

def test_corrupt_price_file_is_backed_up_before_overwrite(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text("{not json", encoding="utf-8")
    pl = PriceList(p)
    assert pl.items == {} and "damaged" in pl.problem
    bad = list(tmp_path.glob("prices.bad-*.json"))
    assert len(bad) == 1 and bad[0].read_text(encoding="utf-8") == "{not json"
    pl.set("2000000", "Red Potion", 50)
    pl.save()
    assert PriceList(p).price("2000000") == 50 and PriceList(p).problem is None
    assert bad[0].exists()
    assert not list(tmp_path.glob("*.tmp"))  # atomic write left nothing behind


def test_bad_price_entries_are_skipped_individually(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps({"2000000": 50, "2000001": "x", "Lemon": {"name": "Lemon", "price": "z"},
                             "2000002": {"name": "Blue Potion", "price": 200}}), encoding="utf-8")
    pl = PriceList(p)
    assert set(pl.items) == {"2000000", "2000002"}
    assert "2 bad prices" in pl.problem and list(tmp_path.glob("prices.bad-*.json"))


def test_corrupt_slot_items_file_is_backed_up(tmp_path):
    p = tmp_path / "slots.json"
    p.write_text(json.dumps([{"slot": 0, "signature": RED, "id": 2000000, "name": "Red Potion"},
                             {"slot": "x", "signature": BLUE, "id": 1, "name": "Bad slot"}]), encoding="utf-8")
    si = SlotItems(p)
    assert [a.name for a in si.slots.values()] == ["Red Potion"] and "1 bad entry" in si.problem
    p.write_text("[[[", encoding="utf-8")
    si = SlotItems(p)
    assert si.slots == {} and si.problem
    assert len(list(tmp_path.glob("slots.bad-*.json"))) >= 1


def test_name_keyed_price_applies_to_id_keyed_usage():
    pl = PriceList()
    pl.set("Red Potion", "Red Potion", 50)  # added while the id lookup failed
    assert spent({"2000000": 3}, pl, {"2000000": "red  potion"}) == (150, 0)
    s = Session(prices=pl)
    s.on_use(2000000, "Red Potion", 3)
    assert s.mesos_spent == 150 and s.unpriced_used == 0
    pl2 = PriceList()
    pl2.set("2000000", "Red Potion", 50)  # and the other way round
    assert spent({"red potion": 2}, pl2) == (100, 0)


def test_dialog_refuses_a_slot_without_a_readable_count(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from mctracker.items_dialog import UNKNOWN, ItemsDialog
    from mctracker.vision.quickslots import SlotRead
    tr = Tracker(prices=PriceList(tmp_path / "p.json"), slot_items=SlotItems(tmp_path / "s.json"))
    icon = np.zeros((32, 32, 3), np.uint8)
    red, empty = np.asarray(RED, np.float32), np.asarray(EMPTY, np.float32)
    tr.slots = [SlotRead(100, red, icon, raw=100), SlotRead(None, red, icon)] + [SlotRead(None, empty, icon)] * 6
    dl = ItemsDialog(tr)
    for slot in (1, 2):  # unreadable count, empty slot
        dl.slot_widgets[slot][2].setEditText("Blue Potion")
        dl._assign(slot)
        assert tr.slot_items.get(slot) is None and dl.slot_widgets[slot][2].currentText() == UNKNOWN
        assert "no readable count" in dl.error.text()
    dl.slot_widgets[0][2].setEditText("Red Potion")
    dl._assign(0)
    assert tr.slot_items.get(0).name == "Red Potion"
    dl.close()
    app.processEvents()


# --- the experimental setting ------------------------------------------------------------


def potting(tr):
    tr.prices.set("2000000", "Red Potion", 50)
    tr.slot_items.assign(0, RED, 2000000, "Red Potion")
    start_session(tr)
    for now in (2, 2.15):  # read twice: counted
        tr.on_message_lines([("You have gained mesos (+300)", 0)], now=now)
    for c in [95] * 5 + [94] * 5 + [92] * 5:
        tr.on_quickslots(reads(c))


def test_settings_persist_atomically_and_ignore_the_old_toggle_key(tmp_path):
    p = tmp_path / "app.json"
    assert AppSettings(p).overlay_scale == 1.0
    st = AppSettings(p)
    st.overlay_scale = 1.5
    st.save()
    assert json.loads(p.read_text()) == {"overlay_scale": 1.5, "overlay_opacity": 100, "follow_game": False}
    assert AppSettings(p).overlay_scale == 1.5
    assert not list(tmp_path.glob("*.tmp"))
    p.write_text("{nope")  # damaged: default, original kept aside
    bad = AppSettings(p)
    assert bad.overlay_scale == 1.0 and bad.problem and list(tmp_path.glob("app.bad-*.json"))


def test_old_settings_file_with_the_experimental_key_loads(tmp_path):
    for old in (True, False, "junk"):  # whatever the old key holds, it is ignored
        p = tmp_path / "app.json"
        p.write_text(json.dumps({"experimental_items_used": old, "overlay_scale": 1.2}))
        st = AppSettings(p)
        assert st.overlay_scale == 1.2 and st.problem is None
        assert not hasattr(st, "experimental_items_used")
        st.save()
        assert "experimental_items_used" not in json.loads(p.read_text())
    assert not list(tmp_path.glob("app.bad-*.json"))


def test_a_fresh_session_tracks_potions_and_net_mesos_by_default(tmp_path):
    tr = Tracker(save_dir=tmp_path)  # default settings
    potting(tr)
    s = tr.session
    assert tr.slots and s.used == {2000000: 3}
    assert s.mesos_spent == 150 and s.net_mesos == 150
    tr.on_map("Town", [])
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert saved["mesos_spent"] == 150 and saved["net_mesos"] == 150 and saved["used"] == {"Red Potion": 3}
    assert tr.today()["mesos_spent"] == 150 and tr.today()["net_mesos"] == 150


def test_a_session_with_no_uses_still_saves_used_fields(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    start_session(tr)
    for now in (2, 2.15):  # read twice: counted
        tr.on_message_lines([("You have gained mesos (+300)", 0)], now=now)
    tr.on_map("Town", [])
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert saved["used"] == {} and saved["used_list"] == [] and saved["mesos_spent"] == 0
    assert saved["used_unpriced"] == 0 and "items_used_estimate" not in saved
    s = history.load(tmp_path)[0]
    assert s.has_items_used and s.potions == 0 and s.net_mesos == 300


def test_hand_booked_uses_count_without_any_setting():
    s = Session()
    s.on_use(2000000, "Red Potion", 4)
    assert s.used == {2000000: 4}


def test_capture_always_reads_quickslots(monkeypatch):
    from mctracker import capture

    class Stop(Exception):
        pass

    made = []
    monkeypatch.setattr(capture, "Reader", lambda: SimpleNamespace(
        ocr=None, read_messages=lambda f, s: None, read_messages_fast=lambda f, s: None, read_status=lambda f, s: None, read_map=lambda f, s: None,
        read_party=lambda f, s: None, read_chat=lambda f, s: None))
    monkeypatch.setattr(capture, "read_status", lambda reader, f, s: None)  # PR #7: status read in capture.py
    monkeypatch.setattr(capture, "QuickslotReader", lambda ocr: made.append(1) or SimpleNamespace(read=lambda f: []))
    w = capture.CaptureWorker()
    w._frame, w._running = np.zeros((4, 4, 3), np.uint8), True
    state = {"n": 0}

    def sleep(_):
        state["n"] += 1
        if state["n"] > 2:
            w._running = False
    monkeypatch.setattr(capture.time, "sleep", sleep)
    w._run()
    assert made == [1]  # always on: no setting gates the reader


def test_dialog_always_shows_the_slots(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from mctracker.items_dialog import ItemsDialog
    tr = Tracker(prices=PriceList(tmp_path / "p.json"), slot_items=SlotItems(tmp_path / "s.json"))
    dl = ItemsDialog(tr)
    dl.show()
    assert dl.slots_box.isVisibleTo(dl)
    assert not hasattr(dl, "off_note")
    assert dl.table is not None and dl.new_price.isEnabled()
    dl.close()
    app.processEvents()


def test_dialog_accepts_a_fresh_drag_with_an_unconfirmed_count(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    widgets.QApplication.instance() or widgets.QApplication([])
    from mctracker.items_dialog import ItemsDialog
    from mctracker.vision.quickslots import SlotRead
    tr = Tracker(prices=PriceList(tmp_path / "p.json"), slot_items=SlotItems(tmp_path / "s.json"))
    icon = np.zeros((32, 32, 3), np.uint8)
    empty = np.asarray(EMPTY, np.float32)
    tr.slots = [SlotRead(None, np.asarray(RED, np.float32), icon, raw=100)] + [SlotRead(None, empty, icon)] * 7
    dl = ItemsDialog(tr)
    dl.slot_widgets[0][2].setEditText("Red Potion")
    dl._assign(0)  # count not confirmed yet (two reads needed) but the raw read is there
    assert tr.slot_items.get(0).name == "Red Potion"
    dl.close()


def test_todays_totals_add_up_old_sessions_without_spent_data(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    day = datetime.now().strftime("%Y%m%d")
    old = {"map": "Old", "started_at": datetime.now().isoformat(timespec="seconds"), "active_seconds": 60,
           "exp_gained": 10, "party_kills": 1, "mesos": 400}  # no mesos_spent / used at all
    (tmp_path / f"{day}-1.json").write_text(json.dumps(old))
    new = {**old, "mesos": 300, "mesos_spent": 150, "items_used_estimate": True}
    (tmp_path / f"{day}-2.json").write_text(json.dumps(new))
    tr._load_today()
    t = tr.today()
    assert t["mesos"] == 700 and t["mesos_spent"] == 150 and t["net_mesos"] == 550


# --- phantom uses (owner bug 2026-11-25: lemons -2 -> -54 in one tick) -----------------
#
# The reader caches OCR per digit pixels, so a misread sticks to a count for as
# long as it shows. When two consecutive counts both stick wrong ("8", "7" for
# 108, 107: the leading digits lost) they are each stable and linked to each
# other, so the endpoint check passed and the segment booked 110 -> 7, clamped
# to K per tick of the segment (about 52), and the count coming back looked like
# a restock that locked it in.

LEMONS = [*held(110, 60), *held(109, 10), *held(108, 10)]  # 2 real uses over 80 ticks


def test_two_reads_misread_the_same_way_book_nothing_big():
    assert uses([*LEMONS, *held(M(8)), *held(M(7))]) <= 2 + K  # was 54
    assert uses([*LEMONS, *held(M(8)), *held(M(7)), *held(107), *held(106)]) <= 4  # was 55


def test_misread_run_that_comes_back_is_dropped():
    assert uses([*LEMONS, *held(M(8)), *held(M(7)), *held(108)]) == 2
    assert uses([*LEMONS, *held(M(8), 20), *held(M(7), 20), *held(M(6), 20), *held(107)]) == 3
    assert uses([*held(60, 30), *held(M(160)), *held(M(159)), *held(59)]) == 1  # a spike run


def test_drop_faster_than_k_per_tick_is_never_uses():
    # A stack moved, dropped or a slot partly hidden ("58" for 158): none of it is used.
    assert uses([*held(158, 30), *held(58), *held(57)]) == 1
    assert uses([*held(158, 30), *held(58), *held(57), *held(157)]) <= 2
    assert uses([*held(120, 30), *held(60, 30)]) == 0  # dropped 60 on the floor


def test_restock_then_use_back_down_still_counts():
    # Runs are only dropped when they are left by a jump: real use back down is linked.
    assert uses([*held(20), *held(120), *range(119, 15, -1), *held(15)]) == 105


def test_steady_potting_still_counts():
    assert uses([*held(110, 20), *range(109, 59, -1), *held(60)]) == 50


def test_restock_potted_down_then_seen_after_a_gap_is_kept():
    # Review of #52: the run 120 -> 50 came back near 20, but at a plausible rate across a gap.
    assert uses([*held(20), *held(120), *range(119, 49, -1), *held(50), *held(None, 10), *held(25)]) == 70


def test_misread_pair_after_a_gap_waits_for_confirmation():
    lead = [*held(110, 30), *held(108, 10), *held(None, 40)]
    assert uses([*lead, *held(M(8)), *held(M(7))]) <= 2 + K  # was 103 while showing
    assert uses([*lead, *held(M(8)), *held(M(7)), *held(107)]) == 3
    # Known limit: a misread pair still showing CONFIRM ticks later, never coming back
    # before a pause or the end, is booked. Each misread is one count's pixels, so the
    # next use normally reads right and the run is dropped.


def test_net_mesos_cant_swing_from_one_misread():
    import random
    rng = random.Random(7)
    for _ in range(300):
        base = rng.randint(30, 250)
        gap = rng.choice([0, 0, 5, 34, 60])
        lead = [*held(base, 40), *held(base - 1, 10), *held(None, gap)]
        wrong = [rng.choice([base // 10, base % 100, base - 50, base + 100]) for _ in range(2)]
        hold = (lambda: rng.randint(S, 8)) if gap else (lambda: rng.randint(S, 30))
        shown = [*lead, *(x for w in wrong for x in held(M(w), hold()))]
        assert uses(shown) <= 1 + K, (base, gap, wrong)  # while the misreads show
        assert uses([*shown, *held(base - 2)]) <= 2 + K, (base, gap, wrong)


def test_pickups_offset_by_earned_lines():
    s = [*held(50), *held(49), *held(50), *held(49)]  # 49 -> 50 was a loot
    it = ItemSeries("lemon", "Lemon")
    for t, v in enumerate(s):
        it.add(t, v)
    assert it.uses() == 1  # the pickup netted a use away
    it.pickup(10, 1)
    assert it.uses() == 2


def test_pickups_never_create_uses():
    it = ItemSeries("lemon", "Lemon")
    for t, v in enumerate([*held(50), *held(51), *held(52)]):
        it.add(t, v)
    it.pickup(5, 1)
    it.pickup(10, 1)
    assert it.uses() == 0
    it = ItemSeries("lemon", "Lemon")
    for t, v in enumerate([*held(50), *held(49)]):
        it.add(t, v)
    it.pickup(3, 40)  # a misresolved loot line: capped by the drops seen
    assert it.uses() == 1


def test_session_loot_reaches_the_quickslot_series():
    si = assigned(RED_SLOT)
    s = Session(mobs=tuple(HUNT))
    s.slot_items = si
    counts = [*held(50), *held(49), *held(50), *held(49)]
    for t, c in enumerate(counts):
        if t == 10:
            s.on_loot(2000000, "Red Potion", 1)
        s.on_quickslots(t, [(c, RED, 0)])
    assert s.used[2000000] == 2
