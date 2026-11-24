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
