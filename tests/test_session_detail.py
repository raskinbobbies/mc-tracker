"""Session detail: saved per-minute EXP and level data, and what the history detail shows or hides."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from mctracker import history  # noqa: E402
from mctracker.exp_table import EXP_TABLE, bar_percent  # noqa: E402
from mctracker.gui import MINUS, EntryGrid, GridRow, SessionDetail, best_index, fmt_full, pct_text, signed  # noqa: E402
from mctracker.kills import Mob  # noqa: E402
from mctracker.overlay import Tile, fmt_num  # noqa: E402
from mctracker.session import Tracker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FakeAssets:
    def mob_sprite(self, key, cb):
        pass

    def item_icon(self, key, cb):
        pass


def saved(**data) -> history.SavedSession:
    return history.SavedSession(Path("x.json"), {"started_at": "2026-11-25T10:00:00", **data})


def detail(**data) -> SessionDetail:
    d = SessionDetail(FakeAssets(), lambda s: None)
    d.show_session(saved(**data))
    return d


RICH = {
    "map": "Ellinia : Forest of Patience", "active_seconds": 600, "exp_gained": 60_000, "party_kills": 100,
    "level_start": 52, "level": 53, "levels_gained": 1, "exp_percent_start": 80.0, "exp_percent": 10.0,
    "exp_per_minute": [5000, 9000, 6000, 0, 40_000], "unassigned_kills": 4, "party_size": 2,
    "kill_list": [{"id": 54, "name": "Glowshroom", "count": 60}],
    "loot_list": [{"id": 4000001, "name": "Snail Shell", "count": 30}],
    "used_list": [{"id": 2000000, "name": "Red Potion", "count": 120}, {"id": None, "name": "Cheese", "count": 5}],
    "mesos": 90_000, "mesos_spent": 12_000, "net_mesos": 78_000,
}


# --- data side ---------------------------------------------------------------------------------


def grind(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Henesys : Hunting Ground", [Mob(54, "Glowshroom", 82)])
    total = 100_000
    tr.on_status(45, total, bar_percent(total, EXP_TABLE[45]), now=0)
    for i, v in enumerate([82, 98, 98], start=1):
        tr.on_message_lines([(f"You received EXP (+{v})", 0.0)], now=i)
        total += v
        tr.on_status(45, total, bar_percent(total, EXP_TABLE[45]), now=i)
    live = tr.session
    minutes = live.exp_per_minute
    tr.on_map("Town", [])
    return minutes


def test_per_minute_exp_and_level_are_saved_and_loaded(tmp_path):
    live_minutes = grind(tmp_path)
    (s,) = history.load(tmp_path)
    assert s.exp_per_minute == live_minutes and sum(live_minutes) > 0
    assert s.level_end == 45
    assert s.data["level_start"] == 45
    start_pct = bar_percent(100_000, EXP_TABLE[45])
    assert s.data["exp_percent_start"] == pytest.approx(start_pct, abs=0.01)
    assert s.data["exp_percent"] > s.data["exp_percent_start"]
    lvl0, p0, lvl1, p1, gained = s.level_span
    assert (lvl0, lvl1) == (45, 45) and gained == pytest.approx(p1 - p0)


def test_best_minute_is_the_first_highest():
    assert saved(exp_per_minute=[5, 40, 9, 40]).best_minute == (1, 40)
    assert saved(exp_per_minute=[0, 0]).best_minute is None
    assert saved(exp_per_minute=[]).best_minute is None


def test_old_file_fallbacks():
    old = saved(map="Old", active_seconds=60, exp_gained=600)
    assert old.exp_per_minute is None and old.best_minute is None
    assert old.level_span is None
    assert old.items_value is None and old.potions is None
    assert old.unassigned_kills == 0 and not old.party
    assert saved(exp_per_minute="junk").exp_per_minute is None
    assert saved(level=53, levels_gained=1).level_span is None  # level but no percents: not enough for the bar


def test_level_span_counts_whole_levels():
    s = saved(**RICH)
    assert s.level_span == (52, 80.0, 53, 10.0, 30.0)
    # An older file with the end level but no level_start: derive it from the levels gained.
    s2 = saved(level=53, levels_gained=2, exp_percent_start=50.0, exp_percent=25.0)
    assert s2.level_span == (51, 50.0, 53, 25.0, 175.0)


def test_street_and_map_name_split():
    assert saved(map="Ellinia : Forest of Patience").street_and_map == ("Ellinia", "Forest of Patience")
    assert saved(map="Primeval Forest I").street_and_map == ("", "Primeval Forest I")
    assert saved().street_and_map == ("", "Unknown map")


def test_items_value_only_when_tracked():
    assert saved(**RICH).items_value == 12_000
    assert saved(mesos=500).items_value is None  # old file: no items-used data


def test_signed_entries_and_full_commas():
    assert signed(1234) == "+1,234" and signed(-1234) == f"{MINUS}1,234" and signed(0) == "0"
    assert fmt_full(99_999) == "99,999" and fmt_full(123_456) == "123.45K"


# --- widget visibility ---------------------------------------------------------------------------


def test_rich_session_shows_every_section(app):
    d = detail(**RICH)
    assert all(not w.isHidden() for w in (d.level_bar, d.chart_section, d.loot_section, d.killed_section, d.foot_box))
    assert "Forest of Patience" in d.title.text() and "Ellinia" in d.title.text()
    assert "Lv 52" in d.level_bar.text.text() and "Lv 53" in d.level_bar.text.text()
    assert d.level_bar.gain.text() == "+30.0%"
    assert "best 40,000 (minute 5)" in d.chart_section.caption.text()


def tiles_of(grid) -> list[Tile]:
    return [c if isinstance(c, Tile) else c.findChild(Tile) for c in grid._tiles]  # the shared boxed tile


def test_loot_grid_has_mesos_and_loot_and_used_has_its_own_section(app):
    d = detail(**RICH)
    loot = tiles_of(d.loot)
    assert [t.count.text() for t in loot] == ["90K", "30"]  # mesos, loot: nothing used in here
    assert [t.count.property("tone") or "" for t in loot] == ["gain", "gain"]
    used = tiles_of(d.used)
    assert [t.count.text() for t in used] == [f"{MINUS}120", f"{MINUS}5"]
    assert [t.count.property("tone") or "" for t in used] == ["loss", "loss"]
    assert all(t.badge for t in loot + used)
    assert d.loot_section.caption.text() == "1 kind" and d.used_section.caption.text() == "125 used"
    assert not d.used_section.isHidden()
    d.show_session(saved(**{**RICH, "used_list": []}))
    assert d.used_section.isHidden()


def test_killed_list_and_unknown_footer(app):
    d = detail(**RICH)
    assert not d.killed.footer.isHidden() and d.killed.footer.text() == "4 unknown shared kills"
    d.show_session(saved(**{**RICH, "unassigned_kills": 0, "party_size": 1}))
    assert d.killed.footer.isHidden()
    d.show_session(saved(**{**RICH, "kill_list": [], "unassigned_kills": 0}))
    assert d.killed_section.isHidden()
    d.show_session(saved(**{**RICH, "kill_list": [], "unassigned_kills": 3, "party_size": 1}))
    assert not d.killed_section.isHidden() and d.killed.footer.text() == "3 unknown kills"


def test_footer_stats_signs_and_rates(app):
    d = detail(**RICH)
    f = d.foot
    assert set(f) == {"MESOS", "POTIONS", "NET"}  # POTIONS is the mesos spent: no second ITEMS box
    assert f["MESOS"].val.text() == "+90,000" and f["MESOS"].val.property("tone") == "gain"
    assert f["MESOS"].rate.text() == "+9,000/min"
    assert f["POTIONS"].val.text() == f"{MINUS}12,000" and f["POTIONS"].val.property("tone") == "loss"
    assert f["POTIONS"].rate.text() == f"{MINUS}1,200/min"
    assert f["NET"].val.text() == "+78,000" and f["NET"].rate.text() == "+7,800/min"
    losing = saved(active_seconds=60, mesos=100, mesos_spent=600, net_mesos=-500, used_list=[])
    d.show_session(losing)
    assert f["NET"].val.text() == f"{MINUS}500" and f["NET"].val.property("tone") == "loss"
    assert f["POTIONS"].val.text() == f"{MINUS}600"
    d.show_session(saved(active_seconds=60, mesos=100))  # old file: no items-used data, so no POTIONS
    assert f["POTIONS"].isHidden() and not f["MESOS"].isHidden() and not f["NET"].isHidden()


def test_money_boxes_add_up_and_never_guess_prices(app):
    d = detail(map="A : B", active_seconds=170, mesos=1202, mesos_spent=1600, net_mesos=-398,
               used_list=[{"id": 2010005, "name": "Lemon", "count": 7}, {"id": 2010003, "name": "Meat", "count": 6}])
    f = d.foot
    assert (f["MESOS"].val.text(), f["POTIONS"].val.text(), f["NET"].val.text()) == ("+1,202", f"{MINUS}1,600", f"{MINUS}398")
    assert d.used_section.caption.text() == "13 used"  # the counts stay with the USED row
    d.show_session(saved(active_seconds=170, mesos=1202, mesos_spent=1600, net_mesos=-398, used_unpriced=2, used_list=[]))
    assert f["POTIONS"].val.text() == f"{MINUS}1,600+"  # items with no price: the figure is a floor
    # counts only (saved before prices): no POTIONS figure, NET is the mesos picked up, flagged in its tooltip
    d.show_session(saved(active_seconds=170, mesos=1202, used={"Meat": 6, "Lemon": 7}))
    assert f["POTIONS"].isHidden() and f["NET"].val.text() == "+1,202" and f["NET"].toolTip()
    assert d.used_section.caption.text() == "13 used" and not d.used_section.isHidden()


def test_old_minimal_file_hides_everything_missing_without_crashing(app):
    d = detail(map="Old", active_seconds=600, exp_gained=9000, party_kills=40, kills={"Snail": 40})
    assert d.level_bar.isHidden() and d.chart_section.isHidden()
    assert d.loot_section.isHidden() and d.foot_box.isHidden()
    assert not d.killed_section.isHidden() and d.killed.footer.isHidden()
    assert d.title.text() == "Old"


def test_garbage_fields_do_not_crash(app):
    d = detail(map=None, active_seconds=0, exp_per_minute=["a", None], level="53",
               exp_percent_start="?", used_list=[], loot_list=[{"id": 1, "name": "A", "count": 0}])
    assert d.chart_section.isHidden() and d.level_bar.isHidden()
    d.show_session(None)
    assert d.chart_section.isHidden() and d.foot_box.isHidden()


def test_missing_per_minute_hides_chart_but_keeps_level_bar(app):
    d = detail(**{k: v for k, v in RICH.items() if k != "exp_per_minute"})
    assert d.chart_section.isHidden() and not d.level_bar.isHidden()
    d2 = detail(**{k: v for k, v in RICH.items() if k not in ("exp_percent_start", "exp_percent")})
    assert d2.level_bar.isHidden() and not d2.chart_section.isHidden()


def test_chart_and_level_bar_paint(app, capsys):
    d = detail(**RICH)
    d.resize(420, 800)
    d.show()
    d.grab()  # paint errors are printed, not raised
    assert capsys.readouterr().err == ""


# --- review fixes ----------------------------------------------------------------------------------


def test_fmt_num_truncates_never_rounds_up():
    assert fmt_num(999_999) == "999.99K"
    assert fmt_num(1_999_999) == "1.99M"
    assert fmt_num(100_000) == "100.00K"
    assert fmt_num(1_000_000) == "1.00M"
    assert fmt_num(-1_999_999) == "-1.99M" and fmt_num(999) == "999"


def test_percent_text_truncates_and_zero_has_no_sign(app):
    assert pct_text(66.85) == "66.8" and pct_text(47.5) == "47.5" and pct_text(0.09) == "0.0"
    d = detail(**{**RICH, "level": 52, "level_start": 52, "levels_gained": 0,
                  "exp_percent_start": 50.0, "exp_percent": 50.04})
    assert d.level_bar.gain.text() == "0.0%" and not d.level_bar.gain.property("tone")
    d.show_session(saved(**{**RICH, "level": 52, "level_start": 52, "levels_gained": 0,
                            "exp_percent_start": 50.0, "exp_percent": 49.96}))
    assert d.level_bar.gain.text() == "0.0%"  # -0.04 shows as 0.0%, not -0.0%
    d.show_session(saved(**{**RICH, "exp_percent_start": 0.0, "exp_percent": 66.85, "level_start": 53}))
    assert d.level_bar.gain.text() == "+66.8%"


def test_no_rates_before_the_warm_up_minute(app):
    d = detail(active_seconds=15, exp_gained=4000, exp_per_minute=[4000], mesos=4000, mesos_spent=0, net_mesos=4000,
               used_list=[{"id": 1, "name": "Red Potion", "count": 3}])
    assert d.headline.text() == "+4.00K EXP" and "EXP/hr" not in d.stats.text()  # earned so far, not a rate
    assert d.foot["MESOS"].val.text() == "+4,000" and d.foot["MESOS"].rate.text() == ""
    assert d.foot["POTIONS"].rate.text() == "" and d.foot["NET"].rate.text() == ""
    assert d.chart.avg == 0 and d.chart_section.caption.text() == ""
    d.show_session(saved(**RICH))  # 10 minutes: everything is back
    assert d.chart.avg > 0 and d.chart_section.caption.text() and d.foot["MESOS"].rate.text() == "+9,000/min"
    assert d.headline.text().endswith("EXP/min") and "—" not in d.headline.text()


def test_null_fields_read_as_zero(app):
    s = saved(unassigned_kills=None, party_size=None, exp_per_minute=None, mesos=5)
    assert s.unassigned_kills == 0 and not s.party
    assert saved(unassigned_kills="x", party_size="2").unassigned_kills == 0
    d = detail(active_seconds=600, unassigned_kills=None, party_size=None, kill_list=[{"id": 1, "name": "A", "count": 2}])
    assert d.killed.footer.isHidden()


def test_best_index_is_shared():
    from mctracker import gui, history as h

    assert gui.best_index is h.best_index is best_index
    assert best_index([1, 5, 2, 5]) == 1 and best_index([0, 0]) is None


# --- the owner's saved sessions: junk loot names, layout ------------------------------------------------

BANANA = [{"id": 4000031, "name": "Lupin's Banana", "count": 8}, {"id": 4031149, "name": "MilkJug", "count": 2},
          {"id": 4031146, "name": "Silver Ore", "count": 3}, {"id": 4000030, "name": "Lupin Doll", "count": 2},
          {"id": 4000050, "name": "Raffle Petal", "count": 4}, {"id": 4000060, "name": "Arrows for Crossbow", "count": 5}]


def junk(name: str, count: int = 1) -> dict:
    return {"id": None, "name": name, "count": count}


@pytest.mark.parametrize("damaged, want", [
    ("xiaobanmiapupin's Banana", "Lupin's Banana"),  # another player's name glued on the front
    ("oin's Banana", "Lupin's Banana"),  # the first letters clipped
    ("lilk Jug", "MilkJug"),
    ("Silyer Ore", "Silver Ore"),
    ("Rafile Petel", "Raffle Petal"),  # two letters misread
    ("Raffile Petal", "Raffle Petal"),
    ("Arrows for Cros...", "Arrows for Crossbow"),  # cut off by the chat box
])
def test_damaged_loot_names_fold_into_the_item(damaged, want):
    s = saved(loot_list=[*BANANA, junk(damaged, 2)])
    loot, unrecognised = s.loot_view()
    by_name = {e.name: e.count for e in loot}
    base = {e["name"]: e["count"] for e in BANANA}
    assert unrecognised == [] and by_name[want] == base[want] + 2  # the counts add, nothing is invented
    assert len(loot) == len(BANANA)  # no tile of its own


def test_unmatched_names_are_counted_not_tiled():
    s = saved(loot_list=[*BANANA, junk("shr..."), junk("Zzzzzzzz Qqq", 3)])
    loot, unrecognised = s.loot_view()
    assert len(loot) == len(BANANA) and sum(e.count for e in unrecognised) == 4


def test_two_equally_close_items_are_not_guessed():
    known = [{"id": 1, "name": "Blue Mushroom", "count": 1}, {"id": 2, "name": "Blue Mushrooms", "count": 1}]
    loot, unrecognised = saved(loot_list=[*known, junk("Blue Mushroomz")]).loot_view()
    assert [e.count for e in loot] == [1, 1] and [e.name for e in unrecognised] == ["Blue Mushroomz"]


def test_a_short_known_name_does_not_swallow_a_longer_item():
    from mctracker.history import match_known_name

    known = {1: "Banana", 2: "Lupin's Banana"}
    assert match_known_name("xiaobanmiapupin's Banana", known) == 2
    assert match_known_name("Pie Flour", {1: "Pie Flour"}) == 1 and match_known_name("Pie Flour", {1: "Rye Flour"}) is None


def test_file_with_no_item_ids_keeps_its_names():
    loot, unrecognised = saved(loot={"Snail Shell": 3, "Mushroom Spore": 1}).loot_view()
    assert [(e.name, e.count) for e in loot] == [("Snail Shell", 3), ("Mushroom Spore", 1)] and unrecognised == []


def test_item_search_repairs_misread_names(monkeypatch):
    from mctracker import gamedata

    db = [{"id": 1, "name": "Silver Ore"}, {"id": 2, "name": "Silver Mine Map"}, {"id": 3, "name": "Raffle Petal"},
          {"id": 4, "name": "Arrows for Crossbow"}, {"id": 5, "name": "Arrows for Bow"}, {"id": 6, "name": "Milk Jug"}]
    monkeypatch.setattr(gamedata, "_search_items", lambda q: [h for h in db if q.lower() in h["name"].lower()])
    gamedata.item_id.cache_clear()
    assert gamedata.item_id("Silyer Ore") == 1
    assert gamedata.item_id("Rafile Petel") == 3 and gamedata.item_id("Raffile Petal") == 3
    assert gamedata.item_id("lilk Jug") == 6
    assert gamedata.item_id("Arrows for Cros...") == 4
    assert gamedata.item_id("shr...") is None
    gamedata.item_id.cache_clear()


def test_detail_shows_the_owner_session_without_junk_tiles(app):
    d = detail(map="Hidden Street : Monkey Forest I", active_seconds=170, mesos=1202, mesos_spent=1600, net_mesos=-398,
               loot_list=[{"id": 4000031, "name": "Lupin's Banana", "count": 8},
                          junk("xiaobanmiapupin's Banana"), junk("oin's Banana"), junk("shr...")],
               used_list=[{"id": 2010005, "name": "Lemon", "count": 7}, {"id": 2010003, "name": "Meat", "count": 6}])
    loot = tiles_of(d.loot)
    assert [t.count.text() for t in loot] == ["1,202", "10"]  # mesos, Lupin's Banana 8 + 1 + 1
    assert d.loot_section.caption.text() == "1 kind · 1 unrecognised"
    assert "shr..." in d.loot_section.caption.toolTip()
    assert d.title.text().startswith("Monkey Forest I<br>") and "Hidden Street" in d.title.text()
    assert "played" in d.when.text() and "grinding" not in d.when.text()


def test_unknown_item_is_a_placeholder_tile_with_the_name_in_the_tooltip(app):
    grid = EntryGrid("loot", FakeAssets())
    grid.show_rows([GridRow(None, "Mystery Thing", "3", "gain", full="+3"), GridRow(5, "Known", "9", "gain")])
    a, b = tiles_of(grid)
    assert a.size() == b.size() and "Mystery Thing" in a.toolTip()
    assert a.img.pixmap() is not None and not a.img.pixmap().isNull()
    assert not any(isinstance(c, QLabel) and c.objectName() == "muted" for c in grid.findChildren(QLabel))  # no captions


def test_tiles_flow_into_as_many_columns_as_fit(app):
    grid = EntryGrid("loot", FakeAssets())
    grid.show_rows([GridRow(i, f"Item {i}", str(i), "gain") for i in range(1, 25)])
    narrow, wide = 330, 1500
    assert grid.grid.heightForWidth(wide) < grid.grid.heightForWidth(narrow)  # more columns when there is room
    grid.setFixedWidth(wide)
    grid.grid.setGeometry(grid.rect())
    assert len({t.y() for t in tiles_of(grid)}) == 1  # all 24 fit one row at 1500px
    grid.setFixedWidth(narrow)
    grid.grid.setGeometry(grid.rect())
    xs = sorted({t.x() for t in tiles_of(grid)})
    assert len(xs) >= 3 and all(b - a == xs[1] - xs[0] for a, b in zip(xs, xs[1:]))  # same pitch: no holes


def test_header_stays_while_the_body_scrolls(app):
    d = detail(**{**RICH, "loot_list": [{"id": i, "name": f"Item {i}", "count": i} for i in range(1, 80)]})
    d.resize(420, 520)
    d.show()
    app.processEvents()
    bar = d.scroll.verticalScrollBar()
    assert bar.maximum() > 0 and d.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    bar.setValue(bar.maximum())
    for w in (d.title, d.when, d.headline, d.foot_box):  # outside the scroll area: they never move
        assert not d.scroll.isAncestorOf(w) and w.isVisible()
    assert d.findChildren(type(d.scroll)) == [d.scroll]  # one scroll area, so no nested scrollbars
    assert d.scroll.widget().width() <= d.scroll.viewport().width()  # nothing wider than the panel: no sideways clipping


def test_chart_has_axis_labels_and_slim_bars_at_any_width(app, capsys):
    from mctracker.gui import ExpChart

    c = ExpChart()
    c.set_data([1000 + 37 * i for i in range(39)], 1200.0)
    for width in (380, 2000):
        c.resize(width, 130)
        c.grab()
        plot_w = width - c.PAD_LEFT - 4
        labels = c.minute_labels(plot_w)
        assert labels[0] == 0 and len(labels) >= 3
        assert plot_w / len(c.values) * (labels[1] - labels[0]) >= 34  # labels never touch
    assert capsys.readouterr().err == ""
    assert ExpChart.BAR_MAX <= 20  # at 2000px a bar is a slim bar, not a block


# --- review: two real items never fold into each other --------------------------------------------------


@pytest.fixture(autouse=True)
def no_disk_item_cache(monkeypatch):
    """The detail panel reads rival names from the user's item cache: keep tests off the real one."""
    from mctracker import gamedata

    monkeypatch.setattr(gamedata, "cached_item_names", lambda: ())


@pytest.mark.parametrize("unknown, known", [
    ("Mushroom Cap", "Orange Mushroom Cap"),  # a real shorter name that is the tail of a longer one
    ("Orange Mushroom Cap", "Mushroom Cap"),
    ("Red Potion", "Super Red Potion"),
    ("Super Red Potion", "Red Potion"),
    ("Scroll for Gloves for ATT 60%", "Scroll for Gloves for ATT 30%"),  # digits are never changed
    ("Scroll for Gloves for ATT 30%", "Scroll for Gloves for ATT 60%"),
    ("Stiff Feathers", "Stiff Feather"),  # a plural is its own item
    ("Stiff Feather", "Stiff Feathers"),
])
def test_real_look_alike_items_stay_separate(unknown, known):
    from mctracker.history import match_known_name

    assert match_known_name(unknown, {1: known}) is None
    loot, unrecognised = saved(loot_list=[{"id": 1, "name": known, "count": 4}, junk(unknown, 2)]).loot_view()
    assert [(e.name, e.count) for e in loot] == [(known, 4)] and [e.name for e in unrecognised] == [unknown]


def test_a_rival_db_name_blocks_the_fold():
    from mctracker.history import match_known_name

    known = {1: "Silver Ore"}
    assert match_known_name("Silyer Ore", known) == 1
    assert match_known_name("Silyer Ore", known, rivals=["Silyer Ore"]) is None  # the name is itself a real item
    assert match_known_name("Silyer Ore", known, rivals=["Silxer Ore"]) is None  # another real item is as close
    assert match_known_name("oin's Banana", {1: "Lupin's Banana"}, rivals=["Coin's Banana"]) is None
    loot, unrecognised = saved(loot_list=[{"id": 1, "name": "Silver Ore", "count": 3}, junk("Silyer Ore")]).loot_view(
        rivals=["Silyer Ore"])
    assert [(e.name, e.count) for e in loot] == [("Silver Ore", 3)] and len(unrecognised) == 1


def test_item_search_keeps_look_alikes_apart(monkeypatch):
    from mctracker import gamedata

    db = [{"id": 1, "name": "Orange Mushroom Cap"}, {"id": 2, "name": "Mushroom Cap"},
          {"id": 3, "name": "Scroll for Gloves for ATT 60%"}, {"id": 4, "name": "Scroll for Gloves for ATT 30%"},
          {"id": 5, "name": "Stiff Feather"}, {"id": 6, "name": "Stiff Feathers"}, {"id": 7, "name": "Red Potion"},
          {"id": 8, "name": "Super Red Potion"}, {"id": 9, "name": "Banana"}, {"id": 10, "name": "Lupin's Banana"}]
    monkeypatch.setattr(gamedata, "_search_items", lambda q: [h for h in db if q.lower() in h["name"].lower()])
    gamedata.item_id.cache_clear()
    try:
        for h in db:  # every real name resolves to itself
            assert gamedata.item_id(h["name"]) == h["id"], h["name"]
        assert gamedata.item_id("Scroll for Gloves for ATT 90%") is None  # no such scroll: no guess
        assert gamedata.item_id("xiaobanmiapupin's Banana") == 10 and gamedata.item_id("oin's Banana") == 10
        assert gamedata.item_id("Stiff Featherz") is None  # as close to both: ambiguous
    finally:
        gamedata.item_id.cache_clear()


def test_offline_item_search_keeps_the_raw_name(monkeypatch):
    from mctracker import gamedata

    monkeypatch.setattr(gamedata, "_search_items", lambda q: [])  # what an HTTP error gives
    gamedata.item_id.cache_clear()
    try:
        assert gamedata.item_id("xiaobanmiapupin's Banana") is None and gamedata.item_id("Silyer Ore") is None
    finally:
        gamedata.item_id.cache_clear()


def test_cached_item_names_read_the_disk_cache_only(monkeypatch, tmp_path):
    import json as _json

    from mctracker import gamedata

    monkeypatch.undo()  # the real function, on a temporary cache folder
    monkeypatch.setattr(gamedata, "_data_dir", lambda: tmp_path)
    gamedata._names_memo.clear()
    (tmp_path / "itemsearch_silver.json").write_text(_json.dumps([{"id": 1, "name": "Silver Ore"}]), encoding="utf-8")
    (tmp_path / "itemsearch_bad.json").write_text("not json", encoding="utf-8")
    assert gamedata.cached_item_names() == ("Silver Ore",)
    gamedata._names_memo.clear()
