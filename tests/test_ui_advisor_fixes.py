"""UI fixes from the 2026-11-26 advisor pass: no-game state, short screens, history columns, compact kills,
headline wording, solo kills, kept selection, saved opacity, wrapped empty-detail hint."""

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from mctracker import history  # noqa: E402
from mctracker.gui import MainWindow, SessionDetail, rate_headline  # noqa: E402
from mctracker.items import AppSettings  # noqa: E402
from mctracker.kills import Mob  # noqa: E402
from mctracker.overlay import Overlay, Tile, kills_summary  # noqa: E402
from mctracker.session import Tracker  # noqa: E402

MOBS = [Mob(6, "Stump", 35), Mob(7, "Slime", 40)]


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


def make(tmp_path, sessions=0):
    d = tmp_path / "sessions"
    d.mkdir(exist_ok=True)
    for i in range(sessions):
        (d / f"s{i}.json").write_text(json.dumps({
            "started_at": f"2026-11-25T1{i}:00:00", "map": "Victoria Road : Forgotten Path of Time III",
            "active_seconds": 3700, "mesos": 1_234_567, "mesos_spent": 23_456, "items_used_estimate": True,
            "used_list": [{"id": 2000001, "name": "Orange Potion", "count": 1234}]}))
    tr = Tracker(save_dir=d, settings=AppSettings(tmp_path / "app.json"))
    tr.on_map("Victoria Road : Forgotten Path of Time III", MOBS)
    tr.on_channel(26)
    assets = FlatAssets(tmp_path)
    ov = Overlay(tr, assets)
    ov.timer.stop()
    w = MainWindow(tr, ov, assets, "alt+a")
    w.timer.stop()
    return tr, ov, w


def pump(app):
    for _ in range(5):
        app.processEvents()


def test_game_closed_shows_no_game_in_both_windows(app, tmp_path):
    tr, ov, w = make(tmp_path)
    ov.show()
    w.show()
    for win in (w, ov):
        win.set_game_attached(False)
    tr.game_closed()
    ov.refresh()
    w._refresh_live()
    assert tr.channel is None  # a restart may land on another channel
    assert tr.map_name  # kept: the capture only re-sends a map that changed
    assert ov.map.text() == "Waiting for MapleStory" and ov.chip.text() == "No game"
    assert not ov.channel.isVisibleTo(ov)
    assert w.live_title.text() == "No game" and not w.channel_pill.isVisibleTo(w)
    assert w.mob_table.rowCount() == 0  # not the last map's monsters
    size = ov.size()
    for win in (w, ov):
        win.set_game_attached(True)
    pump(app)
    assert ov.size() == size  # the overlay keeps one size in every state
    assert "III" in ov.map.text() and "Waiting" not in ov.map.text() and w.mob_table.rowCount() == len(MOBS)


def test_main_window_fits_a_768_screen(app, tmp_path):
    _, _, w = make(tmp_path)
    w.show()
    assert isinstance(w.centralWidget(), QScrollArea)  # a short screen scrolls the page
    assert w.minimumSizeHint().height() < 700
    assert w.mob_table.minimumHeight() < w.mob_table.maximumHeight()  # the monster table can shrink


def test_history_shows_net_mesos_and_potions_without_sideways_scroll(app, tmp_path):
    _, _, w = make(tmp_path, sessions=3)
    w.resize(1100, 860)
    w.show()
    pump(app)
    t = w.table
    hdr = t.horizontalHeader()
    assert not hdr.isSortIndicatorShown()  # a shown (if invisible) indicator widens every column
    sizes = lambda: [hdr.sectionSize(c) for c in t.WRAP_COLS]  # noqa: E731
    one, two = t._wrap_widths(False), t._wrap_widths(True)
    assert all(b < a for a, b in zip(one, two))
    # Too narrow for one line: Net mesos and Potions break at the " · " (the font here is wider than Segoe UI).
    assert sizes() == two
    w.resize(2400, 860)  # room for one line: no wrapping, nothing scrolls sideways
    pump(app)
    assert sizes() == one and t.horizontalScrollBar().maximum() == 0
    assert sum(hdr.sectionSize(c) for c in range(hdr.count())) <= t.viewport().width()


def test_history_keeps_the_selected_session_on_reload(app, tmp_path):
    _, _, w = make(tmp_path, sessions=3)
    w.show()
    w.table.selectRow(2)
    path = w._selected_path()
    w.reload()  # what a save does
    assert w._selected_path() == path and w.table.selectedItems()[0].row() == 2
    assert str(w.detail._session.path) == path


def test_kills_total_and_unmatched_chip_are_compact():
    s = type("S", (), {"total_kills": 12_345, "unassigned_kills": 1_234})()
    figure, caption, tip = kills_summary(s)
    assert figure == "12K" and caption == "kills" and "12,345" in tip  # exact figure in the tooltip
    assert len(kills_summary(type("S", (), {"total_kills": 999_999, "unassigned_kills": 0})())[0]) <= 5


def test_headline_says_last_minute_not_so_far():
    warm = rate_headline(120_000, 61)
    assert "so far" not in warm and "last minute" in warm


def test_detail_says_kills_for_a_solo_session(app):
    solo = SessionDetail(None, lambda s: None)
    solo.show_session(history.SavedSession(Path("x.json"), {"started_at": "2026-11-25T10:00:00", "party_kills": 40}))
    assert "40 kills" in solo.stats.text() and "party" not in solo.stats.text()
    party = SessionDetail(None, lambda s: None)
    party.show_session(history.SavedSession(Path("x.json"), {"started_at": "2026-11-25T10:00:00", "party_kills": 40,
                                                            "party_size": 3}))
    assert "40 party kills" in party.stats.text()


def test_empty_detail_hint_keeps_its_wrapped_height(app):
    d = SessionDetail(None, lambda s: None)
    d.resize(300, 120)  # squeezed
    d.show()
    pump(app)
    assert d.when.height() >= d.when.heightForWidth(d.when.width())


def test_overlay_opacity_is_saved(app, tmp_path):
    tr, ov, _ = make(tmp_path)
    ov.set_opacity(70)
    assert AppSettings(tmp_path / "app.json").overlay_opacity == 70
    ov2 = Overlay(Tracker(settings=AppSettings(tmp_path / "app.json")), FlatAssets(tmp_path))
    assert abs(ov2.windowOpacity() - 0.7) < 0.01
    (tmp_path / "bad.json").write_text(json.dumps({"overlay_opacity": 5}))
    assert AppSettings(tmp_path / "bad.json").overlay_opacity == 100  # out of range: the default


def test_shrunk_sprites_are_smoothed(app):
    img = QImage(28, 28, QImage.Format.Format_RGB32)
    for x in range(28):  # one-pixel stripes: nearest-neighbour keeps pure black and white, smoothing greys them
        for y in range(28):
            img.setPixelColor(x, y, QColor("black" if x % 2 else "white"))
    tile = Tile("t", 1.0, badge=True)
    tile.set_pixmap(QPixmap.fromImage(img))  # 28 px into a 22 px tile (the drawn coin's case)
    out = tile.img.pixmap().toImage()
    assert out.size() == QSize(22, 22)
    assert any(0 < out.pixelColor(x, 11).lightness() < 255 for x in range(22))
