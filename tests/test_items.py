import json
from types import SimpleNamespace

import numpy as np

from mctracker import history
from mctracker.items import PriceList, SlotItems, UsageTracker, spent
from mctracker.kills import Message, Mob, parse_message
from mctracker.session import Session, Tracker
from mctracker.vision.quickslots import digit_mask, parse_count

HUNT = [Mob(54, "Glowshroom", 82)]
RED = [200.0] * 24  # icon fingerprints
BLUE = [40.0] * 24


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


# --- used-count debouncer -----------------------------------------------------------

def feed(u, values, icon=RED):
    return [u.update(0, v, icon) for v in values]


def test_drop_needs_two_identical_reads():
    u = UsageTracker()
    assert feed(u, [100, 100, 99, 99, 99, 97, 97]) == [0, 0, 0, 1, 0, 0, 2]


def test_single_misread_is_ignored():
    u = UsageTracker()
    assert feed(u, [100, 100, 10, 100, 99, 99]) == [0, 0, 0, 0, 0, 1]


def test_increases_and_unreadable_reads_are_ignored():
    u = UsageTracker()
    assert feed(u, [50, 50, None, 49, None, 49, 80, 80, 79, 79]) == [0, 0, 0, 0, 0, 1, 0, 0, 0, 1]


def test_large_drop_is_not_use():
    u = UsageTracker()
    assert sum(feed(u, [200, 200, 100, 100, 99, 99])) == 1


def test_item_swap_resets_slot():
    u = UsageTracker()
    feed(u, [100, 100])
    assert feed(u, [20, 20, 20, 19, 19], icon=BLUE) == [0, 0, 0, 0, 1]
    # a one-off different icon (tooltip over the slot) doesn't reset
    assert u.update(0, 5, RED) == 0
    assert feed(u, [18, 18], icon=BLUE) == [0, 1]


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


def test_tracker_records_quickslot_use_and_saves(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.prices.set("2000000", "Red Potion", 50)
    tr.slot_items.assign(RED, 2000000, "Red Potion")
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_quickslots(reads(100))
    tr.on_quickslots(reads(100))
    tr.on_quickslots(reads(98))
    tr.on_quickslots(reads(98))  # no session yet: baseline only
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([("You received EXP (+82)", 0)], now=1)
    tr.on_message_lines([("You received EXP (+82)", -22), ("You have gained mesos (+300)", 0)], now=2)
    tr.on_quickslots(reads(95))
    tr.on_quickslots(reads(95))
    s = tr.session
    assert s.used == {2000000: 3} and s.mesos == 300 and s.net_mesos == 150
    assert tr.today()["net_mesos"] == 150
    tr.on_map("Town", [])
    saved = history.load(tmp_path)[0]
    assert (saved.mesos, saved.mesos_spent, saved.net_mesos) == (300, 150, 150)
    day = history.by_day([saved])[0]
    assert day.net_mesos == 150
    assert tr.today()["mesos"] == 300 and tr.today()["mesos_spent"] == 150


def test_paused_use_is_not_recorded(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([("You received EXP (+82)", 0)], now=1)
    tr.on_quickslots(reads(10))
    tr.on_quickslots(reads(10))
    tr.pause()
    tr.on_quickslots(reads(8))
    tr.on_quickslots(reads(8))
    tr.resume()
    tr.on_quickslots(reads(7))
    tr.on_quickslots(reads(7))
    assert tr.session.used_other == {"Slot 1 (Shift)": 1}


# --- review fixes: misreads, steady potting, last items ----------------------------

EMPTY = [90.0] * 24  # empty slot background
TIP = [140.0] * 24  # tooltip covering the slot


def running(values):
    out, total = [], 0
    for n in values:
        total += n
        out.append(total)
    return out


def test_stable_misread_drop_is_reverted():
    u = UsageTracker()
    got = feed(u, [18, 18, 13, 13, 17, 17, 16, 16])
    assert sum(got) == 2
    assert min(running(got)) >= 0  # the correction never takes the total below zero
    assert -4 in got  # 18 -> "13" booked 5, then 17 nets it to 1


def test_misread_increase_is_not_a_restock():
    u = UsageTracker()
    assert sum(feed(u, [83, 83, 88, 88, 82, 82])) == 1


def test_dropped_digit_is_suspect():
    assert sum(feed(UsageTracker(), [25, 25, 2, 2, 24, 24])) == 1
    assert sum(feed(UsageTracker(), [25, 25, 2, 2, 2, 2])) == 0
    # ... unless later values follow on from it
    assert sum(feed(UsageTracker(), [25, 25, 2, 2, 1, 1])) == 24
    # small real drops across a digit boundary count straight away
    assert feed(UsageTracker(), [10, 10, 9, 9]) == [0, 0, 0, 1]


def test_steady_potting_counts_every_step():
    u = UsageTracker()
    assert sum(feed(u, [100, 100, *range(99, 59, -1), 60, 60])) == 40
    u = UsageTracker()  # starting mid-run, with no repeat at all
    assert sum(feed(u, [*range(100, 59, -1), 60])) == 40


def test_restock_after_real_use():
    u = UsageTracker()
    assert sum(feed(u, [10, 10, 5, 5, 100, 100])) == 5
    assert sum(feed(u, [99, 99])) == 1


def test_last_items_are_counted_when_slot_empties():
    u = UsageTracker()
    feed(u, [3, 3])
    assert sum(feed(u, [None] * 6, icon=EMPTY)) == 3
    u = UsageTracker()
    feed(u, [8, 8])  # a big stack vanishing is moved/dropped, not used
    assert sum(feed(u, [None] * 6, icon=EMPTY)) == 0


def test_tooltip_does_not_reset_baseline():
    u = UsageTracker()
    feed(u, [50, 50])
    assert feed(u, [None, None, None], icon=TIP) == [0, 0, 0]
    assert sum(feed(u, [49, 49])) == 1
    u = UsageTracker()
    feed(u, [2, 2])
    assert sum(feed(u, [None, None, None], icon=TIP)) == 0
    assert sum(feed(u, [2, 1, 1])) == 1


def test_moved_last_items_are_not_counted():
    u = UsageTracker()
    u.update_all([(2, RED), (None, EMPTY)])
    u.update_all([(2, RED), (None, EMPTY)])
    events = []
    for _ in range(6):
        events += u.update_all([(None, EMPTY), (2, RED)])
    assert events == []


def test_tracker_applies_corrections(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.prices.set("2000000", "Red Potion", 50)
    tr.slot_items.assign(RED, 2000000, "Red Potion")
    tr.on_map("Ant Tunnel", HUNT)
    tr.on_status(45, 1000, 10.0, now=0)
    tr.on_message_lines([("You received EXP (+82)", 0)], now=1)
    for c in [18, 18, 13, 13]:
        tr.on_quickslots(reads(c))
    assert tr.session.used == {2000000: 5}
    for c in [17, 17, 16, 16]:
        tr.on_quickslots(reads(c))
    assert tr.session.used == {2000000: 2} and tr.session.mesos_spent == 100


def test_session_use_never_goes_negative():
    s = Session()
    s.on_use(2000000, "Red Potion", 2)
    s.on_use(2000000, "Red Potion", -5)
    s.on_use_other("Slot 1 (Shift)", -1)
    assert not s.used and not s.used_other and s.mesos_spent == 0


# --- review fixes: settings files and price matching ----------------------------------

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
