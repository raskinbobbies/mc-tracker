"""Overlay content rules: number formats, warm-up, ETA cap, signed counts, overflow, layout size."""

import os
import random
import time
from collections import Counter
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from mctracker.demo import DemoFeed  # noqa: E402
from mctracker.items import AppSettings  # noqa: E402
from mctracker.overlay import (  # noqa: E402
    ETA_CAP_S,
    ICON_SLOTS,
    Overlay,
    Tile,
    fmt_eta,
    fmt_short,
    fmt_signed,
    item_entries,
    mob_entries,
    session_view,
)
from mctracker.rates import delta_tone, last_minute_delta  # noqa: E402
from mctracker.session import Tracker  # noqa: E402

MINUS = "−"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def fake_session(active_s=600, net_exp=9000, minutes=(1000, 2000, 50), mesos=1485, spent=0, potions=0, eta=14_460):
    """Just the Session attributes ``session_view`` reads."""
    exp = SimpleNamespace(net=net_exp)
    s = SimpleNamespace(
        active_s=active_s, exp=exp, level=45, exp_percent=21.9, mesos=mesos, net_mesos=mesos - spent,
        used=Counter({1: potions}) if potions else Counter(), used_other=Counter(), exp_per_minute=list(minutes),
        rate_per_hour=lambda: net_exp / active_s * 3600 if active_s else 0.0,
        session_rate_per_hour=lambda: net_exp / active_s * 3600 if active_s else 0.0,
        seconds_to_level=lambda: eta,
    )
    s.exp.exp_remaining = lambda: 365_877
    return s


# --- number formats -------------------------------------------------------------------------

def test_full_commas_under_100k_then_k_m():
    assert fmt_short(1512) == "1,512"
    assert fmt_short(10_929) == "10,929"
    assert fmt_short(99_999) == "99,999"
    assert fmt_short(100_000) == "100.0K"
    assert fmt_short(125_340) == "125.3K"
    assert fmt_short(1_250_000) == "1.25M"
    assert fmt_short(999_999) == "1.00M"  # not "1000.0K"
    assert fmt_short(2_500_000_000) == "2.50B"
    assert fmt_short(0) == "0"


def test_signs_use_the_real_minus_and_plus_only_when_asked():
    assert fmt_signed(-375) == f"{MINUS}375"
    assert fmt_signed(1485, plus=True) == "+1,485"
    assert fmt_signed(1485) == "1,485"
    assert fmt_signed(0, plus=True) == "0"
    assert fmt_signed(-150_000) == f"{MINUS}150.0K"
    assert fmt_signed(-0.4, plus=True) == "0"  # rounds to nothing: no stray sign


def test_eta_is_a_dash_when_absent_or_absurd():
    assert fmt_eta(None) == "—"
    assert fmt_eta(4 * 3600 + 60) == "4h 01m"
    assert fmt_eta(ETA_CAP_S) == "99h 00m"
    assert fmt_eta(ETA_CAP_S + 1) == "—"
    assert fmt_eta(3014 * 3600 + 60) == "—"  # never "3014h 01m"


# --- the figures ------------------------------------------------------------------------------

def test_nothing_rate_based_shows_before_the_first_minute():
    v = session_view(fake_session(active_s=30, minutes=(500,)))
    assert v.warming and v.rate == v.avg == v.eta == "—" and v.delta is None
    assert v.net_rate == v.potions_rate == "—"
    assert v.net == "+1,485"  # totals are not rates: they show right away


def test_rates_appear_after_a_minute():
    v = session_view(fake_session(active_s=600, net_exp=9000, mesos=1485, spent=500, potions=30))
    assert not v.warming and v.rate == "900" and v.avg == "900" and v.eta == "4h 01m"
    assert v.eta_label == "LV 46 IN"
    assert v.net == "+985" and v.net_rate == "+98/min" and v.net_tone == "gain"  # 985 over 10 minutes
    assert v.potions == f"{MINUS}30" and v.potions_rate == f"{MINUS}3/min" and v.potions_tone == "loss"


def test_eta_and_avg_dash_when_rate_is_nothing_or_level_is_far():
    assert session_view(fake_session(eta=None)).eta == "—"
    assert session_view(fake_session(eta=3014 * 3600)).eta == "—"
    assert session_view(fake_session(net_exp=0)).avg == "—"


def test_tones_follow_the_sign():
    v = session_view(fake_session(mesos=0, spent=250))
    assert v.net == f"{MINUS}250" and v.net_tone == "loss" and v.net_rate.startswith(MINUS)
    v = session_view(fake_session(mesos=0, spent=0))
    assert v.net == "0" and v.net_tone == "" and v.potions == "0" and v.potions_tone == ""


def test_vs_average_badge_uses_the_main_window_rule():
    # last finished minute 2000 against the average of everything else (900/min)
    v = session_view(fake_session(active_s=130, net_exp=3050, minutes=(1000, 2000, 50)))
    assert v.delta == ("▲122%", "over")
    v = session_view(fake_session(active_s=130, net_exp=1150, minutes=(1000, 100, 50)))
    assert v.delta == ("▼89%", "under")
    v = session_view(fake_session(active_s=130, net_exp=1959, minutes=(1000, 909, 50)))  # +1%: at average
    assert v.delta[1] == "near"
    assert session_view(fake_session(active_s=100, minutes=(1000, 50))).delta == ("—", "near")  # one minute finished
    assert last_minute_delta([100, 112, 7], 100) == 12 and delta_tone(12) == "over"
    assert delta_tone(-2) == delta_tone(2) == "near" and delta_tone(-3) == "under"


# --- icon rows ---------------------------------------------------------------------------------

NAMES = {("item", 1): "Snail Shell", ("item", 2): "Cheese", ("mob", 6): "Stump"}


def test_item_row_signs_loot_used_and_mesos():
    entries, more = item_entries(1052, Counter({1: 14}), Counter({2: 3}), NAMES)
    assert more == 0
    assert [(e.key, e.text, e.tone) for e in entries] == [
        (("mesos",), "1,052", ""), (("loot", 1), "14", ""), (("used", 2), f"{MINUS}3", "loss")]
    assert "Cheese" in entries[2].tip


def test_item_row_has_no_empty_tiles():
    assert item_entries(0, Counter(), Counter(), {}) == ([], 0)
    assert item_entries(0, Counter({1: 0}), Counter({2: 0}), {}) == ([], 0)


def test_item_row_same_item_looted_and_used_keeps_both():
    entries, _ = item_entries(0, Counter({2: 5}), Counter({2: 3}), NAMES)
    assert [e.key for e in entries] == [("loot", 2), ("used", 2)]


def test_item_row_overflow_tile_counts_what_is_left_out():
    loot = Counter({i: 100 - i for i in range(1, 9)})
    entries, more = item_entries(500, loot, Counter(), {})
    assert len(entries) == ICON_SLOTS - 1  # the last slot is the "+N" tile
    assert more == 9 - (ICON_SLOTS - 1)  # mesos + 8 items, 4 shown
    assert entries[0].key == ("mesos",) and entries[1].key == ("loot", 1)  # biggest first
    just_fits = item_entries(500, Counter({i: 5 for i in range(ICON_SLOTS - 1)}), Counter(), {})
    assert len(just_fits[0]) == ICON_SLOTS and just_fits[1] == 0


def test_monster_row_counts_and_unknown_chip():
    entries, more, note = mob_entries(Counter({6: 30, 7: 4}), NAMES, 0)
    assert [(e.key, e.text) for e in entries] == [(("mob", 6), "×30"), (("mob", 7), "×4")] and not more and not note
    kills = Counter({i: 50 - i for i in range(10)})
    entries, more, note = mob_entries(kills, {}, 12)
    assert note == "+12 unknown"
    assert len(entries) + (1 if more else 0) <= ICON_SLOTS - 1  # the chip takes a slot
    assert len(entries) + more == 10
    assert mob_entries(Counter(), {}, 0) == ([], 0, "")


# --- the widget ----------------------------------------------------------------------------------

class FlatAssets:
    def __init__(self, tmp_path):
        self.path = tmp_path / "sprite.png"
        pm = QPixmap(40, 40)
        pm.fill(QColor("#4a90d9"))
        pm.save(str(self.path))

    def mob_sprite(self, _id, cb):
        cb(str(self.path))

    item_icon = mob_sprite


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    return now


def run_demo(tmp_path, clock, seconds, scale=1.0):
    random.seed(7)
    settings = AppSettings(tmp_path / "app.json")
    settings.overlay_scale = scale
    tr = Tracker(save_dir=tmp_path / "sessions", settings=settings)
    feed = DemoFeed(tr)
    feed.timer.stop()
    ov = Overlay(tr, FlatAssets(tmp_path))
    ov.timer.stop()
    for _ in range(seconds):
        clock[0] += 1
        feed.tick()
    ov.show()
    ov.refresh()
    QApplication.processEvents()
    return tr, ov


def texts(ov):
    return [lbl.text() for lbl in ov.findChildren(QLabel) if lbl.isVisibleTo(ov)]


def test_warming_up_overlay_shows_dashes_and_the_hint(app, tmp_path, clock):
    _, ov = run_demo(tmp_path, clock, 20)
    assert ov.rate.text().startswith("—") and "warming up" in ov.delta.text()
    assert ov.avg_val.text() == ov.eta_val.text() == "—" and not ov.sep.isVisibleTo(ov)


def test_vs_avg_badge_shows_a_dash_once_warm_but_not_yet_comparable(app, tmp_path, clock):
    assert session_view(fake_session(active_s=30)).delta is None  # still warming up
    _, ov = run_demo(tmp_path, clock, 65)  # a minute in: warm, but no finished minute to judge yet
    assert ov.delta.isVisibleTo(ov) and ov.sep.isVisibleTo(ov)
    assert ov.delta.text().startswith("—") and "VS AVG" in ov.delta.text()
    assert "warming up" not in ov.delta.text() and ov.delta.property("tone") in ("", None)


@pytest.mark.parametrize("seconds", [0, 20, 200])  # waiting, warming up, running
def test_minimap_prompt_takes_over_the_title_without_changing_the_height(app, tmp_path, clock, seconds):
    _, ov = run_demo(tmp_path, clock, seconds)
    size, title, clock_shown = ov.size(), ov.map.text(), ov.clock.isVisibleTo(ov)
    msg = "Open the minimap (top left) so the map can be read"
    ov.set_minimap_prompt(msg)
    QApplication.processEvents()
    assert ov.map.toolTip() == msg and ov.map.text() != title and not ov.clock.isVisibleTo(ov)
    assert ov.size() == size
    ov.refresh()  # the once-a-second refresh keeps it
    assert ov.map.toolTip() == msg and ov.size() == size
    ov.set_minimap_prompt(None)
    QApplication.processEvents()
    assert ov.map.text() == title and ov.clock.isVisibleTo(ov) == clock_shown and ov.size() == size


def test_minimap_prompt_repeats_do_no_layout_work(app, tmp_path, monkeypatch):
    ov = Overlay(Tracker(save_dir=tmp_path / "sessions", settings=AppSettings()), FlatAssets(tmp_path))
    calls = []
    monkeypatch.setattr(ov.map, "set_full", lambda *a, **k: calls.append(a))
    for _ in range(5):  # hidden overlay, status arriving every 0.4 s
        ov.set_minimap_prompt("Turn on map names in the minimap")
    assert len(calls) == 1
    for _ in range(5):
        ov.set_minimap_prompt(None)
    assert len(calls) == 2


def test_running_overlay_has_one_icon_row_and_no_section_labels(app, tmp_path, clock):
    _, ov = run_demo(tmp_path, clock, 200)
    assert not ov.rate.text().startswith("—") and "/min" in ov.rate.text()
    shown = " ".join(texts(ov))
    for gone in ("LOOT", "USED", "Nothing picked up", "No kills yet", "EXP/hr", "(est.)", "~"):
        assert gone not in shown, gone
    assert ov.items.isVisibleTo(ov) and ov.kills.isVisibleTo(ov)
    assert ov.net_val.text().startswith(("+", MINUS, "0")) and ov.pot_val.text()


def test_empty_rows_are_hidden_not_labelled(app, tmp_path, clock):
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings())
    ov = Overlay(tr, FlatAssets(tmp_path))
    ov.timer.stop()
    ov.show()
    ov.refresh()  # no session yet
    assert not ov.items.isVisibleTo(ov) and not ov.kills.isVisibleTo(ov) and not ov.panel.isVisibleTo(ov)
    assert not any("LOOT" in t or "Nothing" in t for t in texts(ov))


@pytest.mark.parametrize("seconds", [1, 30, 180])
def test_overlay_stays_compact_at_100_percent(app, tmp_path, clock, seconds):
    _, ov = run_demo(tmp_path, clock, seconds)
    assert ov.width() <= 400 and ov.height() <= 240, ov.size()  # about the footprint of maplestory.watch


def test_overlay_frame_is_opaque(app, tmp_path, clock):
    _, ov = run_demo(tmp_path, clock, 5)
    qss = ov.frame.styleSheet()
    assert "#overlay { background: #fff8e8;" in qss  # no rgba(): the game never shows through


def test_badge_tile_pins_its_count_inside_and_widens_for_long_counts(app):
    tile = Tile("t", badge=True)
    tile.set_count("×3")
    short = tile.width()
    tile.set_count("123.4K", "loss")
    assert tile.width() >= short and tile.rect().contains(tile.count.geometry())
    assert tile.count.property("tone") == "loss"
    plain = Tile("t")  # the main window's tile still puts the count under the icon
    plain.set_count("×3")
    assert plain.count.text() == "×3" and plain.layout().indexOf(plain.count) >= 0
    assert tile.layout().indexOf(tile.count) == -1  # the badge floats over the icon instead


def test_elided_title_keeps_the_numeral_and_shows_the_full_name_in_the_tooltip(app):
    from mctracker.overlay import ElidedLabel

    lbl = ElidedLabel(keep_tail=r"(?:\s[IVXivxl|1!]{1,5})?(?:\s\(\?\))?")
    lbl.set_full("Primeval Forest I", "Shallow Passage : Primeval Forest I")
    assert lbl.toolTip() == "Shallow Passage : Primeval Forest I"
    assert lbl._elided(lbl.full_width()) == "Primeval Forest I"
    cut = lbl._elided(lbl.full_width() - 20)
    assert cut.endswith(" I") and cut != "Primeval Forest I" and ("…" in cut or "..." in cut)  # the font may lack "…"
    lbl.set_full("Tree Dungeon, Forest Up North VII (?)")
    assert lbl._elided(lbl.full_width() * 2 // 3).endswith(" VII (?)")
    assert lbl._elided(lbl.full_width() + 10) == "Tree Dungeon, Forest Up North VII (?)"
    assert not lbl._elided(10).endswith(" (?)")  # too narrow for a tail: plain elision


def test_status_hint_does_not_squeeze_the_map_name(app, tmp_path):
    # #owner 2026-11-25: "Not a hunting map" beside the title cut the name to "Pr...".
    def title(map_name, mobs):
        tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
        tr.on_map(map_name, mobs)
        ov = Overlay(tr, FlatAssets(tmp_path))
        ov.timer.stop()
        ov.show()
        ov.refresh()
        QApplication.processEvents()
        return ov

    from mctracker.kills import Mob

    name = "Shallow Passage : Primeval Forest I"
    hunting = title(name, [Mob(1, "Glowshroom", 82)])
    idle = title(name, [])  # not a hunting map: the hint says so
    assert idle.size() == hunting.size()
    assert idle.map.text().endswith(" I") and idle.map.text().startswith("Primeval")
    assert idle.map.toolTip().startswith(name)
    assert "Not a hunting map" in idle.map.toolTip() + idle.clock.text()
    assert len(idle.map.text()) >= 10  # the name, not "Pr…"
