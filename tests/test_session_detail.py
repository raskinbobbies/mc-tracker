"""Session detail: saved per-minute EXP and level data, and what the history detail shows or hides."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker import history  # noqa: E402
from mctracker.exp_table import EXP_TABLE, bar_percent  # noqa: E402
from mctracker.gui import MINUS, SessionDetail, fmt_full, signed  # noqa: E402
from mctracker.kills import Mob  # noqa: E402
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
    assert fmt_full(99_999) == "99,999" and fmt_full(123_456) == "123.46K"


# --- widget visibility ---------------------------------------------------------------------------


def test_rich_session_shows_every_section(app):
    d = detail(**RICH)
    assert all(not w.isHidden() for w in (d.level_bar, d.chart_section, d.loot_section, d.killed_section, d.foot_box))
    assert "Forest of Patience" in d.title.text() and "Ellinia" in d.title.text()
    assert "Lv 52" in d.level_bar.text.text() and "Lv 53" in d.level_bar.text.text()
    assert d.level_bar.gain.text() == "+30.0%"
    assert "best 40,000 (minute 5)" in d.chart_section.caption.text()


def test_loot_grid_is_signed_with_mesos_tile_and_kinds_caption(app):
    d = detail(**RICH)
    texts = [t.count.text() for t in d.loot._tiles]
    assert texts == ["+90,000", "+30", f"{MINUS}120", f"Cheese\n{MINUS}5"]  # mesos, loot, used
    tones = [t.count.property("tone") or "" for t in d.loot._tiles]
    assert tones == ["gain", "", "loss", "loss"]
    assert d.loot_section.caption.text() == "3 kinds"
    assert "USED" not in "".join(w.text() for w in d.findChildren(type(d.title)))


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
    assert f["MESOS"].val.text() == "+90,000" and f["MESOS"].val.property("tone") == "gain"
    assert f["MESOS"].rate.text() == "+9,000/min"
    assert f["POTIONS"].val.text() == f"{MINUS}125" and f["POTIONS"].val.property("tone") == "loss"
    assert f["ITEMS"].val.text() == f"{MINUS}12,000" and f["ITEMS"].rate.text() == f"{MINUS}1,200/min"
    assert f["NET"].val.text() == "+78,000" and f["NET"].rate.text() == "+7,800/min"
    losing = saved(active_seconds=60, mesos=100, mesos_spent=600, net_mesos=-500, used_list=[])
    d.show_session(losing)
    assert f["NET"].val.text() == f"{MINUS}500" and f["NET"].val.property("tone") == "loss"
    assert f["ITEMS"].val.text() == f"{MINUS}600"
    d.show_session(saved(active_seconds=60, mesos=100))  # old file: no items-used data, so no POTIONS or ITEMS
    assert f["POTIONS"].isHidden() and f["ITEMS"].isHidden() and not f["MESOS"].isHidden()


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
