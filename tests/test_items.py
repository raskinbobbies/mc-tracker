import json
from types import SimpleNamespace

import numpy as np
import pytest

from mctracker import history
from mctracker.items import ItemSeries, PriceList, QuickslotUsage, SlotItems, spent
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
# Notation in the table: one value per tick (quickslot read); None is a gap; a
# string is a misread. Values are what reaches the series, i.e. already
# confirmed by two identical reads (that step is tested on its own below).

M = str  # marks a misread in the tables


def uses(values):
    """Uses for one item read once per tick."""
    it = ItemSeries("red", "Red")
    for t, v in enumerate(values):
        if v is not None:
            it.add(t, int(v))
    return it.uses()


def held(v, n):
    return [v] * n


@pytest.mark.parametrize("values, expected", [
    ([18, 18, 13, 13, 17, 17, 16, 16], {2}),  # stable misread "13" corrected by net difference
    ([83, *held(M(88), 70), 82], {1}),  # misread up-step is not a restock
    ([18, *held(M(13), 70), 17, 16], {2}),  # ... however long the misread lasts
    ([18, *held(M(13), 70), 16, 15], {3}),
    ([25, *held(M(2), 30), 24], {0, 1}),  # dropped digit
    ([*range(100, 59, -1), *held(60, 10)], {40}),  # steady potting
    # Changed: was {3, 4} (gave 4). 48 -> 49 is an exact return to the value
    # before the last down-step, so that step is cancelled: 3, an undercount by
    # one if the 49 really was loot. Loot is still never netted beyond that.
    ([50, 49, 48, 49, 48, 47], {3}),
    ([3, *held(None, 6), 3, 3, 2], {1}),  # tooltip over the slot is a gap
    ([120, M(12), 119], {0, 1}),
    ([*range(50, 40, -1), 140, *range(139, 129, -1)], {18, 19}),  # restock mid-pot
], ids=["18-13-17-16", "83-88-82", "18-13x70-17", "18-13x70-16", "25-2-24", "100-to-60",
        "loot", "tooltip", "120-12-119", "restock"])
def test_usage_table(values, expected):
    assert uses(values) in expected


def test_usage_never_overcounts_misreads():
    assert uses([25, *held(M(2), 30), 24]) <= 1
    assert uses([25, *held(M(2), 30)]) == 0  # an open suspect is dropped
    assert uses([50, 49, 48, 49, 48, 47]) <= 4
    assert uses([*range(50, 40, -1), 140, *range(139, 129, -1)]) <= 19


def test_sell_then_partial_rebuy():
    # 120 -> 12 (sold) -> 100 (bought back) looks exactly like 120, "12", 100.
    # Changed: this used to book the net 20 when 20 uses fit in the time it
    # took. A step is now worth at most K per observed change, and a reconnect
    # must land within K of the anchor, so neither books anything.
    assert uses([120, *held(12, 10), 100]) == 0
    assert uses([120, 12, 100]) == 0
    assert uses([120, *held(12, 10), 100, 99]) == 1  # only the use after the rebuy
    # Selling happens in town, and a non-hunting map ends the session, so in
    # practice a session never sees it.


def test_real_big_drops_and_restocks_rebase():
    assert uses([200, 100, 99]) == 1  # dropped 100 on the floor, then a use
    assert uses([10, 5, 100, 99]) == 1  # 10 -> 5 in one tick is suspect and never booked; 100 -> 99 is
    assert uses([10, 9, 8, 7, 6, 5, 100, 100, 99]) == 6
    assert uses([10, None, 8]) == 2  # a step of at most K is booked across a gap too


# Review of c044c68: every booked amount is bounded by K per observed change,
# never scaled by the length of a gap.

def test_misread_right_after_a_long_gap_is_bounded():
    # 120, covered for 40 ticks, uncovered at 118 but misread "12", then 117, 116:
    # used to book 109. "12" reconnects to the pre-gap 120 and books 3, then 1.
    assert uses([120, *held(None, 40), M(12), 117, 116]) == 4
    assert uses([121, 120, *held(None, 40), M(12), 117, 116]) == 5  # same with a trusted anchor


def test_misread_right_after_a_short_gap_is_bounded():
    # 20, a 6-tick tooltip, "2", 19, 18: used to book 19.
    assert uses([20, *held(None, 6), M(2), 19, 18]) == 2


def test_big_step_across_a_gap_books_one_change_at_most():
    # A real drop across a gap is booked as min(step, K), and only once the
    # reading after the gap is confirmed by a following plausible step.
    assert uses([121, 120, *held(None, 40), 80]) == 1  # 80 unconfirmed: suspect, dropped
    assert uses([121, 120, *held(None, 40), 80, 79]) == 1 + 3 + 1
    assert uses([120, *held(None, 40), 80, 79]) == 1  # provisional anchor: the jump is never booked
    assert uses([121, 120, *held(None, 40), 80, 40, 39]) == 2  # not the reading after the gap: rebase only


def test_stable_misread_at_segment_start_is_provisional():
    # True 31 reads "81" for 20 ticks (carried in from before), then 30, 29:
    # used to book 53. The jump from a provisional anchor books nothing.
    assert uses([*held(M(81), 20), 30, 29]) == 1
    assert uses([*held(M(81), 20), 30, 31, 30]) == 1  # loot after it doesn't reconnect to 81


def test_one_digit_edit_excludes_identity():
    from mctracker.items import _near
    assert 30 not in _near(30) and {31, 80, 3, 130, 300} <= _near(30)
    # the edit must map the suspect into [reading, anchor]: "7" can't be 19 or 20
    assert uses([21, 20, M(7), 19]) == 1  # 21 -> 20 only; 19 is left as the new suspect value


def test_real_big_drop_then_use_rebases():
    # 200, a real drop to 20 held for 65 ticks, then 19, 18: used to book 182.
    assert uses([200, *held(20, 65), 19, 18]) == 2
    assert uses([201, 200, *held(20, 65), 19, 18]) == 3  # trusted anchor, no gap: the drop still books 0


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


def test_slot_emptying_books_nothing():
    # We can't tell an empty slot from a moved stack, so the last few go uncounted.
    u = usage(assigned(RED_SLOT), [[(3, RED)], [(3, RED)], [(None, EMPTY)], [(None, EMPTY)]])
    assert u.uses() == []


def test_unassigned_slots_are_not_tracked():
    u = usage(assigned(), [[(c, RED), (c, BLUE)] for c in [10, 9, 8]])
    assert u.uses() == [] and u.items == {}
    u = usage(assigned(RED_SLOT), [[(c, RED), (c, BLUE)] for c in [10, 9, 8]])
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
    for t, c in enumerate([50, 50]):
        u.record(t, [(c, RED, 0)], si)
    for t, c in enumerate(range(100, 90, -1), start=2):  # orange dragged onto red's slot, then used
        u.record(t, [(c, BLUE, 0)], si)
    assert u.uses() == [] and list(u.items) == ["redpotion"]
    si.assign(0, BLUE, 2000001, "Orange Potion")  # the user reassigns the slot
    for t, c in enumerate([91, 90, 89], start=12):
        u.record(t, [(c, BLUE, 0)], si)
    assert booked(u) == {"Orange Potion": 2}


def test_still_stack_flickering_down_and_back_books_nothing():
    # A cursor resting on a still 100 makes it read "98" for 2 reads every 3
    # minutes, for an hour: used to book 2 per flicker (38). The return to
    # exactly 100 cancels the down-step.
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
    ticks = [[(50, RED)], [(50, RED)], *[[(None, HOVER)]] * 8, [(49, RED)], [(48, RED)]]
    u = usage(assigned(RED_SLOT), ticks)
    assert booked(u) == {"Red Potion": 2} and len(u.items) == 1


def test_same_item_in_two_slots_counts_once():
    si = assigned(RED_SLOT, (1, RED, 2000000, "Red Potion"))
    u = usage(si, [[(c, RED), (c, RED)] for c in [50, 49, 48]])
    assert booked(u) == {"Red Potion": 2} and len(u.items) == 1
    # slots that disagree make that tick a gap. 50 -> 45 across it is a
    # suspect, and books min(5, K) only once 45 is confirmed by a plausible step.
    u = usage(si, [[(a, RED), (b, RED)] for a, b in [(51, 51), (50, 50), (48, 45), (45, 45)]])
    assert booked(u) == {"Red Potion": 1}
    u.record(4, [(44, RED, 0), (44, RED, 0)], si)
    assert booked(u) == {"Red Potion": 1 + 3 + 1}


def test_lone_slot_of_a_two_slot_item_must_agree_with_its_last_value():
    si = assigned(RED_SLOT, (1, RED, 2000000, "Red Potion"))
    u = usage(si, [[(50, RED), (None, EMPTY)], [(49, RED), (None, EMPTY)]])
    assert u.uses() == []  # no committed value yet: a lone slot is never trusted
    u = usage(si, [[(50, RED), (50, RED)], [(49, RED), (None, HOVER)], [(30, RED), (None, HOVER)],
                   [(48, RED), (None, HOVER)], [(47, RED), (47, RED)]])
    assert booked(u) == {"Red Potion": 3}  # 50 -> 49 -> 48 -> 47; the lone "30" is a gap


def test_moved_stack_is_untracked_until_reassigned():
    si = assigned(RED_SLOT)
    u = usage(si, [[(20, RED), (None, EMPTY)], [(None, EMPTY), (20, RED)], [(None, EMPTY), (19, RED)]])
    assert u.uses() == []
    si.assign(0, None, None, "")  # unassign slot 1, assign slot 2
    si.assign(1, RED, 2000000, "Red Potion")
    for t, c in enumerate([19, 18], start=3):
        u.record(t, [(None, EMPTY, 0), (c, RED, 0)], si)
    # same item, so the same series: 20 (slot 1) -> 19 (slot 2) -> 18
    assert booked(u) == {"Red Potion": 2}


def test_exact_return_cancels_a_recent_down_step():
    assert uses([100, 98, 98, 100]) == 0
    assert uses([100, 99, 98, 99]) == 1  # only the last down-step is undone
    assert uses([100, 98, 97, 100]) == 3  # not a return to the value before the last step
    assert uses([100, 98, 99]) == 2  # a gain that isn't an exact return is ignored
    assert uses([100, *held(98, 31), 100]) == 2  # too long ago: a real use, then loot
    assert uses([100, 98, 100, 98, 100]) == 0
    # cancelling restores "provisional": 81 is not trusted, so the jump across the gap books nothing
    assert uses([81, 80, 81, *held(None, 5), 70, 69]) == 1
    assert uses([82, 81, 80, *held(None, 5), 70, 69]) == 2 + 3 + 1  # trusted: min(jump, K) for the gap


def test_confirmed_values_are_backdated():
    # 90 first showed at tick 2, confirmed at 3: the run starts at 2, so the
    # step 100 -> 90 is tick to tick, not across a gap.
    # Changed: a big step across a gap used to be judged as K per elapsed tick
    # (100 -> 90 over four ticks booked 10). Now it books min(step, K) once
    # confirmed, so backdating decides whether that gap booking applies.
    def series(*adds):
        it = ItemSeries("red", "Red")
        for a in adds:
            it.add(*a)
        return it
    it = series((0, 101), (1, 100), (3, 90, 1), (4, 89))
    assert it.segments[0][2][:2] == [2, 3] and it.uses() == 1 + 0 + 1  # no gap: the big drop books 0
    it = series((0, 101), (1, 100), (3, 90), (4, 89))  # not backdated: a gap at tick 2
    assert it.uses() == 1 + 3 + 1


def test_usage_is_cached_by_series_length():
    it = ItemSeries("red", "Red")
    for t, v in enumerate([10, 9, 9, 9]):
        it.add(t, v)
    assert it.uses() == 1 and len(it.segments[0]) == 2  # run-length encoded
    it.segments[0][1][2] = 5  # tamper: same length, cached result
    assert it.uses() == 1


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
    # reaches the series; one 100 -> 60 jump would be a suspect worth at most K
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
    tr.on_message_lines([("You received EXP (+82)", -22), ("You have gained mesos (+300)", 0)], now=2)
    for c in [95, 94, 94, 92]:  # a fresh baseline at 95
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
    for c in [8, 7, 6]:
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


def test_open_suspect_is_dropped_at_session_end(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.slot_items.assign(0, RED, None, "Red Potion")
    start_session(tr)
    for c in [18, 18, 13, 13]:
        tr.on_quickslots(reads(c))
    a = tr.session
    tr.on_map("Other map", HUNT)  # session A ends with "13" still suspect
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
                             {"signature": BLUE, "id": 1, "name": "Old format, no slot"}]), encoding="utf-8")
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
