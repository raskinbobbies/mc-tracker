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
    fmt_tile,
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


def fake_session(active_s=600, net_exp=9000, minutes=(1000, 2000, 50), mesos=1485, spent=0, potions=0, eta=14_460, unpriced=0):
    """Just the Session attributes ``session_view`` reads."""
    exp = SimpleNamespace(net=net_exp)
    s = SimpleNamespace(
        active_s=active_s, exp=exp, level=45, exp_percent=21.9, mesos=mesos, net_mesos=mesos - spent, mesos_spent=spent, unpriced_used=unpriced,
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
    assert v.warming and v.avg == v.eta == "—" and v.delta is None
    assert v.rate == "+9,000" and v.rate_unit == " EXP" and v.hint == "first minute"  # what's been earned so far
    assert v.net_rate == v.potions_rate == "—"
    assert v.net == "+1,485"  # totals are not rates: they show right away


def test_big_figure_switches_from_exp_so_far_to_exp_per_min_at_60s():
    before, after = session_view(fake_session(active_s=59.9)), session_view(fake_session(active_s=60))
    assert (before.rate, before.rate_unit, before.warming, before.hint) == ("+9,000", " EXP", True, "first minute")
    assert (after.rate, after.rate_unit, after.warming, after.hint) == ("9,000", "/min", False, "")
    assert before.eta == before.avg == "—" and before.delta is None  # ETA and vs-average wait for the minute
    assert after.eta != "—" and after.avg != "—"


def test_rates_appear_after_a_minute():
    v = session_view(fake_session(active_s=600, net_exp=9000, mesos=1485, spent=500, potions=30))
    assert not v.warming and v.rate == "900" and v.avg == "900" and v.eta == "4h 01m"
    assert v.eta_label == "LV 46 IN"
    assert v.net == "+985" and v.net_rate == "+98/min" and v.net_tone == "gain"  # 985 over 10 minutes
    # POTIONS is the mesos spent on items used (500 over 10 minutes), not the item count
    assert v.potions == f"{MINUS}500" and v.potions_rate == f"{MINUS}50/min" and v.potions_tone == "loss"


def test_potions_mark_items_with_no_price():
    v = session_view(fake_session(active_s=600, mesos=0, spent=18_340, potions=191, unpriced=3))
    assert v.potions == f"{MINUS}18,340+" and v.potions_rate == f"{MINUS}1,834/min"
    assert "3 items" in v.potions_tip and v.potions_tone == "loss"
    v = session_view(fake_session(active_s=600, mesos=0, spent=0, unpriced=1))
    assert v.potions == "0+" and "1 item " in v.potions_tip
    assert "no price" not in session_view(fake_session(spent=100)).potions_tip


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
        (("mesos",), "1,052", "gain"), (("loot", 1), "14", "gain"), (("used", 2), f"{MINUS}3", "loss")]
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
    big = QLabel(ov.rate.text()).text()
    assert "EXP" in big and "/min" not in big and "—" not in big  # EXP earned so far, not a blank
    assert "first" in ov.delta.text() and "VS AVG" not in ov.delta.text()
    assert ov.avg_val.text() == ov.eta_val.text() == "—" and ov.sep.isVisibleTo(ov)


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


def idle_overlay(tmp_path, map_name="", mobs=(), scale=1.0):
    settings = AppSettings(tmp_path / "app.json")
    settings.overlay_scale = scale
    tr = Tracker(save_dir=tmp_path / "sessions", settings=settings)
    if map_name:
        tr.on_map(map_name, mobs)
    ov = Overlay(tr, FlatAssets(tmp_path))
    ov.timer.stop()
    ov.show()
    ov.refresh()
    QApplication.processEvents()
    return tr, ov


def test_empty_rows_keep_their_space_with_a_quiet_placeholder(app, tmp_path):
    _, ov = idle_overlay(tmp_path)  # no session yet
    assert ov.items.isVisibleTo(ov) and ov.kills.isVisibleTo(ov) and ov.panel.isVisibleTo(ov)
    shown = texts(ov)
    assert "Nothing picked up yet" in shown and "No kills yet" in shown
    assert not any("LOOT" in t or "USED" in t for t in shown)
    assert ov.items.height() == ov.kills.height() > 0


@pytest.mark.parametrize("seconds", [1, 30, 180])
def test_overlay_stays_compact_at_100_percent(app, tmp_path, clock, seconds):
    _, ov = run_demo(tmp_path, clock, seconds)
    assert ov.width() <= 450 and ov.height() <= 240, ov.size()  # about the footprint of maplestory.watch


def test_overlay_is_the_same_size_in_every_state(app, tmp_path, clock):
    """Idle, waiting, not a hunting map, first minute, active, paused, nothing looted yet, long names and values."""
    from mctracker.kills import Mob

    long_name = "Some Street With A Very Long Name : The Longest Map Name In The Whole Of Victoria Island Region IX"
    for scale in (0.6, 0.85, 1.0, 1.15, 1.5):
        sizes = {}
        _, ov = idle_overlay(tmp_path, scale=scale)
        sizes["idle, no map"] = ov.size()
        _, ov = idle_overlay(tmp_path, "Street : Primeval Forest I", scale=scale)
        sizes["not a hunting map"] = ov.size()
        tr, ov = idle_overlay(tmp_path, long_name, [Mob(1, "Snail", 5)], scale=scale)
        sizes["waiting for EXP, long map name"] = ov.size()
        tr.on_channel(40)
        ov.refresh()
        sizes["channel shown"] = ov.size()
        tr.toggle_pause()
        ov.refresh()
        sizes["paused, no session"] = ov.size()
        for secs, tag in ((20, "first minute"), (65, "just warm"), (300, "active")):
            tr, ov = run_demo(tmp_path, clock, secs, scale=scale)
            sizes[tag] = ov.size()
            if secs == 300:
                tr.session.mesos, tr.session.used[2000000] = 123_456_789, 98_765
                ov.refresh()
                sizes["huge values"] = ov.size()
                tr.toggle_pause()
                ov.refresh()
                sizes["paused"] = ov.size()
                tr.toggle_pause()
                tr.on_map(long_name, tr.mobs, refine=False)  # ends the session: back to waiting
                ov.refresh()
                sizes["map changed"] = ov.size()
        assert len(set((s.width(), s.height()) for s in sizes.values())) == 1, (scale, sizes)


def test_overlay_frame_is_opaque(app, tmp_path, clock):
    _, ov = run_demo(tmp_path, clock, 5)
    qss = ov.frame.styleSheet()
    assert "#overlay { background: #fff8e8;" in qss  # no rgba(): the game never shows through


@pytest.mark.parametrize("scale", [0.6, 0.85, 1.0, 1.15, 2.0])
def test_badge_tile_keeps_its_size_and_the_longest_counts_fit_inside(app, scale):
    from PySide6.QtGui import QFontMetrics

    from mctracker.theme import overlay_qss

    tile = Tile("t", scale, badge=True)
    tile.setStyleSheet(overlay_qss(scale))  # the overlay's count font
    tile.set_count("×3")
    size = tile.size()
    tile.count.ensurePolished()
    for text in (fmt_tile(99_999, "×"), fmt_tile(-99_999), fmt_tile(-123_400), fmt_tile(1_050_000), fmt_tile(999_999),
                 fmt_tile(-9_999), fmt_tile(1_234, "×"), "−123K"):
        tile.set_count(text, "loss")
        assert tile.size() == size and tile.rect().contains(tile.count.geometry())  # a count never widens its tile
        advance = QFontMetrics(tile.count.font()).horizontalAdvance(text)
        room = tile.width() - (round(2 * scale) + 1 if scale >= 0.85 else 1)  # at 60% the font is a few px: just on the tile
        assert advance <= room, (text, scale, advance, tile.width())  # nothing runs off the left
    assert tile.count.property("tone") == "loss"
    plain = Tile("t")  # the main window's tile still puts the count under the icon
    plain.set_count("×3")
    assert plain.count.text() == "×3" and plain.layout().indexOf(plain.count) >= 0
    assert tile.layout().indexOf(tile.count) == -1  # the badge floats over the icon instead


def test_kills_slot_agrees_with_itself(app, tmp_path, clock, monkeypatch):
    """"No kills yet" only when the total is 0; kills with no mob tiles say so; unmatched kills are a small +N."""
    tr, ov = run_demo(tmp_path, clock, 180)
    s = tr.session
    assert s.total_kills > 0 and "No kills yet" not in texts(ov)
    if s.unassigned_kills:
        assert ov.kills.note.isVisibleTo(ov) and ov.kills.note.text() == f"+{s.unassigned_kills:,}"
    monkeypatch.setattr(type(s), "kills", property(lambda self: Counter()))  # kills seen, no monster matched
    ov.refresh()
    assert "mobs not identified yet" in texts(ov) and "No kills yet" not in texts(ov) and not ov.kills.tiles
    _, idle = idle_overlay(tmp_path)
    assert "No kills yet" in texts(idle) and "mobs not identified yet" not in texts(idle)


def test_stats_column_shows_one_dash_until_there_is_a_value(app, tmp_path):
    _, ov = idle_overlay(tmp_path)
    assert ov.net_val.text() == ov.pot_val.text() == "—"
    assert not ov.net_sub.isVisibleTo(ov) and not ov.pot_sub.isVisibleTo(ov)


def test_first_minute_exp_after_a_death_has_one_sign():
    v = session_view(fake_session(active_s=30, net_exp=-500, minutes=(-500,)))
    assert v.rate == f"{MINUS}500" and "+−" not in v.extra + v.rate
    from mctracker.gui import exp_so_far

    assert exp_so_far(-500) == f"{MINUS}500 EXP" and exp_so_far(1240) == "+1.24K EXP"


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
    assert idle.chip._full == "Not a hunting map"  # the hint swaps into the chip slot, beside the whole name
    assert len(idle.map.text()) >= 10  # the name, not "Pr…"


@pytest.mark.parametrize("scale", [0.6, 1.0, 1.5])
@pytest.mark.parametrize("channel", [None, 6, 40])
@pytest.mark.parametrize("name", ["Monkey Forest I", "Shallow Passage : Primeval Forest I",
                                  "Some Street With A Very Long Name : The Longest Map Name In The Whole Of Victoria Island VII"])
def test_title_row_widgets_never_overlap(app, tmp_path, clock, name, channel, scale):
    """Map name, channel chip, clock and the three buttons each own a slot, whatever the name, the channel or the state."""
    from PySide6.QtWidgets import QPushButton

    from mctracker.kills import Mob

    for secs in (0, 6, 400):  # waiting, the first seconds ("0m 06s"), a long session ("9h 59m")
        if secs:
            tr, ov = run_demo(tmp_path, clock, secs if secs < 100 else 200, scale=scale)
            tr.on_map(name, tr.mobs, refine=True)
        else:
            tr, ov = idle_overlay(tmp_path, name, [Mob(1, "Snail", 5)], scale=scale)
        tr.on_channel(channel)
        ov.refresh()
        QApplication.processEvents()
        if secs == 400:
            ov.clock.setText("99h 59m")  # the widest clock the format can print
        title = [ov.map, ov.channel, ov.clock, *ov.findChildren(QPushButton)]
        assert len(title) == 6
        rects = [w.geometry() for w in title]  # a hidden chip keeps its slot: it must not be overlapped either
        for i, a in enumerate(rects):
            assert ov.frame.rect().contains(a), (title[i].objectName(), a)
            assert all(not a.intersects(b) for b in rects[i + 1:]), (name, channel, scale, secs, rects)


def test_tile_counts_are_at_most_five_characters_and_truncate():
    assert fmt_tile(1052) == "1,052" and fmt_tile(14) == "14" and fmt_tile(-3) == f"{MINUS}3" and fmt_tile(0) == "0"
    assert fmt_tile(-1052) == f"{MINUS}1.0K" and fmt_tile(9999) == "9,999" and fmt_tile(10_000) == "10K"
    assert fmt_tile(12_345) == "12K" and fmt_tile(123_999) == "123K" and fmt_tile(999_999) == "999K"  # never rounded up
    assert fmt_tile(1_050_000) == "1.0M" and fmt_tile(1_990_000) == "1.9M" and fmt_tile(12_000_000) == "12M"
    assert fmt_tile(5, "×") == "×5" and fmt_tile(1234, "×") == "×1.2K" and fmt_tile(-123_456) == f"{MINUS}123K"
    for n in (0, 7, 99, -99, 999, -999, 1000, -1000, 9999, 12_345, -99_999, 123_456, 1e7, -1e9, 9e11):
        for prefix in (("", "×") if n >= 0 else ("",)):
            assert len(fmt_tile(n, prefix)) <= 5, (n, prefix, fmt_tile(n, prefix))


def test_overlay_text_is_never_under_nine_pixels_at_100_percent(app, tmp_path, clock):
    import re

    from mctracker.theme import overlay_qss

    sizes = [int(m) for m in re.findall(r"font-size:\s*(\d+)px", overlay_qss(1.0))]
    assert sizes and min(sizes) >= 9
    _, ov = run_demo(tmp_path, clock, 30)  # the inline spans (units, hints) are set from code
    html = " ".join(lbl.text() for lbl in ov.findChildren(QLabel))
    inline = [int(m) for m in re.findall(r"font-size:\s*(\d+)px", html)]
    assert inline and min(inline) >= 9
