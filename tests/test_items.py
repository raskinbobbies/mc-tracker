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
    it = ItemSeries(np.asarray(RED, np.float32), 0)
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
    ([50, 49, 48, 49, 48, 47], {3, 4}),  # loot in between is not netted against uses
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


def test_slot_emptying_books_nothing():
    # We can't tell an empty slot from a moved stack, so the last few go uncounted.
    u = QuickslotUsage()
    for t, (c, sig) in enumerate([(3, RED), (3, RED), (None, EMPTY), (None, EMPTY)]):
        u.record(t, [(c, sig, 0)])
    assert u.uses() == []


def test_same_item_in_two_slots_counts_once():
    u = QuickslotUsage()
    for t, c in enumerate([50, 49, 48]):
        u.record(t, [(c, RED, 0), (c, RED, 0)])
    assert [n for _, n in u.uses()] == [2]
    # slots that disagree make that tick a gap. Changed: 50 -> 45 across it is
    # no longer judged over two ticks (it used to book 5). It is a suspect, and
    # books min(5, K) only once 45 is confirmed by a following plausible step.
    u = QuickslotUsage()
    for t, (a, b) in enumerate([(51, 51), (50, 50), (48, 45), (45, 45)]):
        u.record(t, [(a, RED, 0), (b, RED, 0)])
    assert [n for _, n in u.uses()] == [1]
    u.record(4, [(44, RED, 0), (44, RED, 0)])
    assert [n for _, n in u.uses()] == [1 + 3 + 1]


VARIANT = [215.0] * 24  # RED with a hover highlight: too far for SAME_ICON, close enough for a variant


def test_variant_icon_state_merges_into_the_item():
    # 100 down to 60, one per tick, with the highlighted icon every 20th tick:
    # used to book 39 + 20 = 59 on two series. Now one item, 40.
    u = QuickslotUsage()
    for t, c in enumerate(range(100, 59, -1)):
        u.record(t, [(c, VARIANT if t % 20 == 10 else RED, 0)])
    (it, n), = u.uses()
    assert n == 40 and len(it.aliases) == 1


def test_variant_in_another_slot_merges_on_the_same_count():
    u = QuickslotUsage()
    u.record(0, [(50, RED, 0), (None, EMPTY, 0)])
    u.record(1, [(49, RED, 0), (49, VARIANT, 0)])  # same item in slot 2, highlighted
    u.record(2, [(48, RED, 0), (48, VARIANT, 0)])
    assert len(u.items) == 1 and [n for _, n in u.uses()] == [2]


def test_unmerged_variant_is_still_bounded():
    # Even if the variant isn't merged (here: it shows a count far from the
    # item's), its own series is judged per observed change, never per tick.
    u = QuickslotUsage()
    for t, c in enumerate(range(100, 59, -1)):
        u.record(t, [(c + 20 if t % 20 == 10 else c, VARIANT if t % 20 == 10 else RED, 0)])
    assert len(u.items) == 2 and sum(n for _, n in u.uses()) <= 40


def test_different_item_in_the_slot_is_not_merged():
    u = QuickslotUsage()
    u.record(0, [(50, RED, 0)])
    u.record(1, [(49, BLUE, 0)])  # swapped in, count within K, but a different icon
    u.record(2, [(48, BLUE, 0)])
    assert len(u.items) == 2 and [n for _, n in u.uses()] == [1]
    u = QuickslotUsage()
    u.record(0, [(50, RED, 0), (None, EMPTY, 0)])
    u.record(1, [(50, RED, 0), (30, VARIANT, 0)])  # close icon, but RED shows another count
    assert len(u.items) == 2


def test_variant_is_named_like_the_item(tmp_path):
    si = SlotItems(tmp_path / "slots.json")
    si.assign(RED, 2000000, "Red Potion")
    s = Session(slot_items=si)
    s.quickslots.record(0, [(10, VARIANT, 0)])  # first seen highlighted: the series' main fingerprint
    s.quickslots.record(1, [(9, RED, 0)])
    s.quickslots.record(2, [(8, RED, 0)])
    assert s.used == {2000000: 2} and not s.used_other


def test_items_are_keyed_by_icon_not_slot():
    u = QuickslotUsage()
    u.record(0, [(20, RED, 0), (None, EMPTY, 0)])
    u.record(1, [(None, EMPTY, 0), (20, RED, 0)])  # stack moved to slot 2
    u.record(2, [(None, EMPTY, 0), (19, RED, 0)])
    u.record(3, [(30, BLUE, 0), (18, RED, 0)])
    (it, n), = u.uses()
    assert n == 2 and it.slot == 1


def test_confirmed_values_are_backdated():
    # 90 first showed at tick 2, confirmed at 3: the run starts at 2, so the
    # step 100 -> 90 is tick to tick, not across a gap.
    # Changed: a big step across a gap used to be judged as K per elapsed tick
    # (100 -> 90 over four ticks booked 10). Now it books min(step, K) once
    # confirmed, so backdating decides whether that gap booking applies.
    def series(*adds):
        it = ItemSeries(np.asarray(RED, np.float32), 0)
        for a in adds:
            it.add(*a)
        return it
    it = series((0, 101), (1, 100), (3, 90, 1), (4, 89))
    assert it.segments[0][2][:2] == [2, 3] and it.uses() == 1 + 0 + 1  # no gap: the big drop books 0
    it = series((0, 101), (1, 100), (3, 90), (4, 89))  # not backdated: a gap at tick 2
    assert it.uses() == 1 + 3 + 1


def test_usage_is_cached_by_series_length():
    it = ItemSeries(np.asarray(RED, np.float32), 0)
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


def test_slot_items_identify(tmp_path):
    si = SlotItems(tmp_path / "slots.json")
    si.assign(RED, 2000000, "Red Potion")
    again = SlotItems(tmp_path / "slots.json")
    assert again.identify([203.0] * 24).item_id == 2000000
    assert again.identify(BLUE) is None


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
    tr.slot_items.assign(RED, 2000000, "Red Potion")
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


def test_unnamed_icon_is_labelled_by_slot_and_named_later(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    start_session(tr)
    for c in [10, 9, 8]:
        tr.on_quickslots([SimpleNamespace(count=None, signature=EMPTY),
                          SimpleNamespace(count=c, signature=BLUE)])
    assert tr.session.used_other == {"Slot 2 (D)": 2} and not tr.session.used
    tr.slot_items.assign(BLUE, 2000001, "Orange Potion")  # the dialog names it mid-session
    assert tr.session.used == {2000001: 2} and not tr.session.used_other


def test_pause_starts_a_new_segment(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    start_session(tr)
    for c in range(30, 19, -1):  # 30 -> 20: real use
        tr.on_quickslots(reads(c))
    tr.pause()
    for c in [20, 18, 13, 13]:  # not recorded, including the misread
        tr.on_quickslots(reads(c))
    tr.resume()
    for c in [17, 17]:
        tr.on_quickslots(reads(c))
    assert tr.session.used_other == {"Slot 1 (Shift)": 10}  # nothing subtracted, nothing diffed across


def test_open_suspect_is_dropped_at_session_end(tmp_path):
    tr = Tracker(save_dir=tmp_path)
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
    p.write_text(json.dumps([{"signature": RED, "id": 2000000, "name": "Red Potion"}, {"id": 1}]),
                 encoding="utf-8")
    si = SlotItems(p)
    assert [k.name for k in si.known] == ["Red Potion"] and "1 bad entry" in si.problem
    p.write_text("[[[", encoding="utf-8")
    si = SlotItems(p)
    assert si.known == [] and si.problem
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
