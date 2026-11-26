"""Main window live tab: player header, map card, ETA card, items grid, empty states, first-minute rule."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.gui import (  # noqa: E402
    MainWindow,
    eta_text,
    exp_bar_text,
    item_rows,
    map_parts,
    money_view,
    per_min_text,
)
from mctracker.items import AppSettings  # noqa: E402
from mctracker.overlay import Overlay  # noqa: E402
from mctracker.session import Tracker  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class FlatAssets:
    def __init__(self, tmp_path):
        self.path = tmp_path / "sprite.png"
        pm = QPixmap(40, 40)
        pm.fill(QColor("#4a90d9"))
        pm.save(str(self.path))

    def mob_sprite(self, _id, cb):
        cb(str(self.path))

    item_icon = mob_sprite


def window(tmp_path):
    tr = Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))
    tr.on_map("Warning Street : The Burnt Land V", ())
    assets = FlatAssets(tmp_path)
    w = MainWindow(tr, Overlay(tr, assets), assets, "alt+a")
    w.show()
    return tr, w


# --- pure helpers -----------------------------------------------------------------------------

def test_map_parts_splits_street_and_name():
    assert map_parts("Warning Street : The Burnt Land V") == ("Warning Street", "The Burnt Land V")
    assert map_parts("Demo map") == ("", "Demo map")
    assert map_parts("Ant Tunnel : Dangerous Hill (?)") == ("Ant Tunnel", "Dangerous Hill (?)")


def test_per_min_text():
    assert per_min_text(499.4) == "499"
    assert per_min_text(72) == "72"
    assert per_min_text(0.84) == "0.8"
    assert per_min_text(-80.2) == "−80"
    assert per_min_text(1320) == "1,320"


def test_money_view_waits_for_the_first_minute():
    net, pot = money_view(10_929, 1_575, 30)
    assert net == ("+10,929", "", "gain") and pot == ("−1,575", "", "loss")
    net, pot = money_view(10_929, 1_575, 21.9 * 60)
    assert net == ("+10,929", "499/min", "gain")
    assert pot == ("−1,575", "−72/min", "loss")
    net, pot = money_view(-80, 0, 120)
    assert net[0] == "−80" and net[2] == "loss" and pot == ("0", "0.0/min", "")


def test_money_view_marks_unpriced_items():
    net, pot = money_view(-13_000, 18_340, 10 * 60, unpriced=3)
    assert pot == ("−18,340+", "−1,834/min", "loss")
    net, pot = money_view(0, 0, 10 * 60, unpriced=2)  # nothing priced yet, but items were used
    assert pot[0] == "0+" and pot[2] == "loss"
    assert money_view(0, 500, 10 * 60)[1][0] == "−500"  # all priced: no mark


def test_item_rows_loot_green_used_red_most_first():
    names = {("item", 1): "Snail Shell", ("item", 2): "Red Potion"}
    rows, more = item_rows({1: 14}, {2: 15, 3: 1}, names)
    assert more == 0
    assert [(r.id, r.text, r.tone) for r in rows] == [(2, "−15", "loss"), (1, "14", "gain"), (3, "−1", "loss")]
    assert rows[0].name == "Red Potion" and rows[0].full == "15 used" and rows[1].full == "14 picked up"
    assert item_rows({}, {}, {}) == ([], 0)


def test_item_rows_caps_and_counts_the_rest():
    rows, more = item_rows({i: 100 - i for i in range(1, 25)}, {}, {}, limit=18)
    assert len(rows) == 18 and more == 6


def test_exp_bar_text():
    assert exp_bar_text(1234, 5678, 21.73) == "1,234 / 5,678 · 21.73%"
    assert exp_bar_text(None, None, 21.7) == "21.70%"
    assert exp_bar_text(5, None, None) == ""


def test_eta_text_dash_in_the_first_minute():
    assert eta_text(45, 3 * 3600, 30) == ("LEVEL 46 IN", "—", "after the first minute")
    assert eta_text(45, 3 * 3600 + 41 * 60, 600) == ("LEVEL 46 IN", "3h 41m", "at your average")
    assert eta_text(45, None, 600) == ("LEVEL 46 IN", "—", "")
    assert eta_text(None, None, 0)[0] == "NEXT LEVEL IN"


# --- the window -------------------------------------------------------------------------------

def test_no_session_is_not_a_first_minute(app, tmp_path):
    tr, w = window(tmp_path)
    w._refresh_live()
    text = w.live_rate.text()
    assert text == "Not tracking" and "first minute" not in text and "EXP" not in text
    assert w.live_street.text() == "Warning Street" and w.live_title.text() == "The Burnt Land V"
    assert w.kills_caption.text() == "No kills yet"
    assert w.items_empty.isVisibleTo(w) and w.items_empty.text() == "Nothing picked up yet"
    assert not w.items_grid.isVisibleTo(w)
    assert w.eta_val.text() == "—" and w.eta_tail.text() == ""
    assert "Today: nothing tracked yet" in w.live_sub.text()


def test_no_session_money_boxes_are_empty(app, tmp_path):
    tr, w = window(tmp_path)
    w._refresh_live()
    assert w.net_box.val.text() == "—" and w.potion_box.val.text() == "—"
    assert not w.net_box.sub.isVisibleTo(w) and not w.potion_box.sub.isVisibleTo(w)


def test_player_header_is_hidden_until_a_level_is_read(app, tmp_path):
    tr, w = window(tmp_path)
    w._refresh_live()
    assert not w.lv_badge.isVisibleTo(w) and not w.exp_bar.isVisibleTo(w)
    tr.on_status(45, 1234, 21.7)
    w._refresh_live()
    assert w.lv_badge.isVisibleTo(w) and w.lv_badge.text() == "Lv 45" and w.exp_bar.isVisibleTo(w)


def test_no_game_says_so(app, tmp_path):
    tr, w = window(tmp_path)
    tr.on_status(45, 1234, 21.7)
    w.set_game_attached(False)
    assert w.live_title.text() == "No game" and not w.live_street.isVisibleTo(w)
    assert "Waiting for MapleStory" in w.live_sub.text() and "hunting map" not in w.live_sub.text()
    assert not w.lv_badge.isVisibleTo(w) and not w.exp_bar.isVisibleTo(w)  # the last read is stale
    assert w.live_rate.text() == "Not tracking"
    w.set_game_attached(True)  # the game opened
    assert w.live_title.text() == "The Burnt Land V" and w.lv_badge.isVisibleTo(w)


def test_eta_text_without_a_session_has_no_first_minute_wording():
    assert eta_text(45, None, 0, tracking=False) == ("LEVEL 46 IN", "—", "")
    assert eta_text(None, None, 0, tracking=False) == ("NEXT LEVEL IN", "—", "")


def test_a_session_keeps_the_first_minute_rule(app, tmp_path, monkeypatch):
    import time

    from mctracker.demo import DemoFeed

    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    tr, w = window(tmp_path)
    feed = DemoFeed(tr)
    feed.timer.stop()
    for _ in range(5):
        feed.tick()
        clock[0] += 1.0
    assert tr.session is not None
    w._refresh_live()
    text = w.live_rate.text()
    assert "first minute" in text and "so far" not in text
    assert w.eta_tail.text() == "after the first minute"


def test_running_session_fills_header_items_and_money(app, tmp_path, monkeypatch):
    import random
    import time

    from mctracker.demo import DemoFeed

    random.seed(1)
    clock = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    tr, w = window(tmp_path)
    feed = DemoFeed(tr)
    feed.timer.stop()
    for _ in range(15):  # under a minute
        feed.tick()
        clock[0] += 1.0
    s = tr.session
    assert s is not None
    s.on_loot(4000001, "Snail Shell", 3)
    s.on_use(2000000, "Red Potion", 2)
    w._refresh_live()
    assert w.lv_badge.text() == f"Lv {s.level}"
    assert "/" in w.exp_bar.text and "%" in w.exp_bar.text
    assert w.items_grid.isVisibleTo(w) and not w.items_empty.isVisibleTo(w)
    assert w.potion_box.val.text().startswith("−")
    assert not w.net_box.sub.isVisibleTo(w)  # no per-minute figure yet
    assert "first minute" in w.live_rate.text() and "so far" not in w.live_rate.text()
    assert w.eta_val.text() == "—"
    for _ in range(80):  # past it
        feed.tick()
        clock[0] += 1.0
    w._refresh_live()
    assert "EXP/min, so far" in w.live_rate.text()
    assert w.net_box.sub.isVisibleTo(w) and w.net_box.sub.text().endswith("/min")
    assert w.eta_val.text() != "—" and w.eta_tail.text() == "at your average"
