"""Safe mode: keep the overlay off the game, the browser-style overlay, the quiet preset, the capture border check."""

import json
import logging
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, QSize, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker import capture, offgame, toast  # noqa: E402
from mctracker.app import apply_safe_mode  # noqa: E402
from mctracker.items import AppSettings  # noqa: E402
from mctracker.offgame import free_spot, to_logical  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_safe_mode import FlatAssets, make  # noqa: E402

W = Qt.WindowType
LEFT = QRect(0, 0, 1920, 1040)  # one monitor's available area (taskbar below)
RIGHT = QRect(1920, 0, 1920, 1040)
SIZE = QSize(300, 200)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def reset_toasts():
    yield
    toast.set_safe_modes(False, False)
    toast._notice = None


def fake_game(monkeypatch, game, areas=(LEFT,)):
    monkeypatch.setattr(offgame, "game_rect", lambda: game)
    monkeypatch.setattr(offgame, "screen_areas", lambda: list(areas))


def rect(w) -> QRect:
    return QRect(w.frameGeometry().topLeft(), w.frameGeometry().size())


# --- settings ------------------------------------------------------------------

def test_old_settings_files_load_with_the_new_switches_off(tmp_path):
    p = tmp_path / "app.json"
    p.write_text(json.dumps({"safe_hide_overlay": True}))
    s = AppSettings(p)
    assert (s.safe_overlay_off_game, s.safe_browser_overlay) == (False, False)
    assert s.safe_hide_overlay is True and s.problem is None


def test_the_new_switches_round_trip(tmp_path):
    p = tmp_path / "app.json"
    s = AppSettings(p)
    s.safe_overlay_off_game = s.safe_browser_overlay = True
    s.save()
    again = AppSettings(p)
    assert (again.safe_overlay_off_game, again.safe_browser_overlay) == (True, True)
    p.write_text(json.dumps({"safe_browser_overlay": 1}))
    bad = AppSettings(p)
    assert bad.safe_browser_overlay is False and "safe_browser_overlay" in bad.problem


def test_quiet_preset_turns_on_the_browser_style_overlay_kept_off_the_game(qapp, tmp_path):
    from mctracker.gui import MainWindow

    tr, o = make(tmp_path)
    tr.settings.safe_hide_overlay = True
    w = MainWindow(tr, o, FlatAssets(tmp_path), "alt+a")
    w.on_safe_mode_changed = lambda: apply_safe_mode(tr.settings, o)
    quiet = next(a for a in w.safe_menu.actions() if a.text() == "Anti-cheat quiet")
    quiet.trigger()
    saved = AppSettings(tmp_path / "app.json")
    assert (saved.safe_hide_overlay, saved.safe_overlay_off_game, saved.safe_browser_overlay) == (False, True, True)
    assert w.safe_actions["safe_browser_overlay"].isChecked() and not w.safe_actions["safe_hide_overlay"].isChecked()
    assert o.browser and o.off_game and not o.safe_hidden


# --- browser-style overlay -----------------------------------------------------

def test_browser_style_is_on_top_opaque_never_layered_or_click_through(qapp, tmp_path):
    tr, o = make(tmp_path)
    tr.settings.overlay_opacity = 70
    o.set_opacity(70)
    o.set_locked(True, save=False)
    o.show()
    assert o.windowFlags() & W.WindowTransparentForInput and o.windowFlags() & W.Tool
    o.set_safe_window(off_game=False, browser=True)
    f = o.windowFlags()
    assert f & W.WindowStaysOnTopHint  # like a browser's picture-in-picture window
    assert (f & W.WindowType_Mask) == W.Window  # not a Tool window
    assert not f & (W.WindowTransparentForInput | W.FramelessWindowHint)
    assert f & W.WindowTitleHint and f & W.WindowCloseButtonHint and not f & W.WindowMaximizeButtonHint
    assert not o.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert o.windowOpacity() == 1.0 and o.isVisible()
    o.set_opacity(55)  # the menu still saves it, but the window stays opaque
    assert o.windowOpacity() == 1.0 and tr.settings.overlay_opacity == 55
    o.set_safe_window(off_game=False, browser=False)  # turned off: back as it was, still locked
    f = o.windowFlags()
    assert f & W.WindowTransparentForInput and f & W.FramelessWindowHint and (f & W.WindowType_Mask) == W.Tool
    assert o.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert o.windowOpacity() == pytest.approx(0.55, abs=0.01)


def test_browser_style_sends_toasts_to_the_notice_line(qapp, tmp_path):
    _tr, o = make(tmp_path)
    said = []
    apply_safe_mode(AppSettings(tmp_path / "x.json"), o, notice=lambda t, timeout_s=0: said.append(t))
    toast.set_safe_modes(False, True)
    t = toast.Toast(o)
    t.show_message("Copied “Snail Shell”")
    assert said == ["Copied “Snail Shell”"] and not t.isVisible()


# --- placement off the game ----------------------------------------------------

def test_free_spot_keeps_a_fine_position():
    assert free_spot(SIZE, QPoint(10, 10), QRect(500, 0, 1000, 800), [LEFT]) == QPoint(10, 10)


def test_free_spot_snaps_to_the_nearest_edge_on_one_monitor():
    game = QRect(400, 100, 1200, 800)
    spot = free_spot(SIZE, QPoint(450, 300), game, [LEFT])  # just inside the game's left edge
    assert spot == QPoint(400 - offgame.MARGIN - 300, 300)
    spot = free_spot(SIZE, QPoint(1500, 600), game, [LEFT])  # near its right and bottom edges
    assert not QRect(spot, SIZE).intersects(game) and LEFT.contains(QRect(spot, SIZE))


def test_free_spot_uses_another_monitor_when_the_game_is_fullscreen():
    game = QRect(0, 0, 1920, 1080)
    spot = free_spot(SIZE, QPoint(800, 400), game, [LEFT, RIGHT])
    assert RIGHT.contains(QRect(spot, SIZE)) and not QRect(spot, SIZE).intersects(game)
    assert free_spot(SIZE, QPoint(800, 400), game, [LEFT]) is None  # no room anywhere


def test_to_logical_scales_from_the_screens_origin():
    screens = [(QRect(0, 0, 1280, 720), 1.5), (QRect(1920, 0, 1920, 1080), 1.0)]
    assert to_logical((300, 150, 1500, 750), screens) == QRect(200, 100, 800, 400)
    assert to_logical((2000, 0, 2400, 300), screens) == QRect(2000, 0, 400, 300)


def test_overlay_is_moved_off_the_game_when_it_shows(qapp, tmp_path, monkeypatch):
    game = QRect(400, 100, 1200, 800)
    fake_game(monkeypatch, game)
    _tr, o = make(tmp_path)
    o.move(600, 300)
    o.set_safe_window(off_game=True, browser=False)
    o.show()
    assert o.isVisible() and not rect(o).intersects(game) and LEFT.contains(rect(o))


def test_overlay_moves_to_the_other_monitor_and_hides_when_there_is_no_room(qapp, tmp_path, monkeypatch):
    game = QRect(0, 0, 1920, 1080)
    fake_game(monkeypatch, game, (LEFT, RIGHT))
    _tr, o = make(tmp_path)
    o.move(600, 300)
    o.show()
    o.set_safe_window(off_game=True, browser=False)
    assert o.isVisible() and RIGHT.contains(rect(o))
    o.move(600, 300)  # (a test shortcut) back over the game, and the second monitor is gone
    fake_game(monkeypatch, game, (LEFT,))
    o._keep_off_game()
    assert not o.isVisible()  # parked: never shown over the game
    o.show()
    assert not o.isVisible()
    fake_game(monkeypatch, QRect(0, 0, 1000, 700))  # the game was made smaller: room again
    o._keep_off_game()
    assert o.isVisible() and not rect(o).intersects(QRect(0, 0, 1000, 700))
    o.set_safe_window(off_game=False, browser=False)
    assert o.isVisible()


def test_off_the_game_the_overlay_is_never_click_through(qapp, tmp_path, monkeypatch):
    fake_game(monkeypatch, None)
    _tr, o = make(tmp_path)
    o.set_locked(True, save=False)
    o.set_safe_window(off_game=True, browser=False)
    assert not o.windowFlags() & W.WindowTransparentForInput
    assert o.locked  # the user's lock is kept, and comes back with the switch
    o.set_safe_window(off_game=False, browser=False)
    assert o.windowFlags() & W.WindowTransparentForInput


def test_dragging_onto_the_game_is_refused(qapp, tmp_path, monkeypatch):
    game = QRect(400, 100, 1200, 800)
    fake_game(monkeypatch, game)
    _tr, o = make(tmp_path)
    o.move(10, 10)
    o.set_safe_window(off_game=True, browser=False)
    o.show()
    start = o.pos()
    o.drag_to(QPoint(300, 200))  # its right side would cover the game
    assert o.pos() == start
    o.drag_to(QPoint(20, 900))  # below-left of the game: fine
    assert o.pos() == QPoint(20, 900)
    o.set_safe_window(off_game=False, browser=False)
    o.drag_to(QPoint(600, 300))  # switch off: anywhere goes
    assert o.pos() == QPoint(600, 300)


def test_toast_off_the_game_is_placed_beside_it_and_takes_clicks(qapp, tmp_path, monkeypatch):
    game = QRect(0, 0, 1500, 1040)
    fake_game(monkeypatch, game)
    toast.set_safe_modes(True, False)
    t = toast.Toast(None)  # no anchor: it would go top-centre, over the game
    t.show_message("Switching to mesos.watch…")
    assert t.isVisible() and not QRect(t.pos(), t.size()).intersects(game)
    assert not t.windowFlags() & W.WindowTransparentForInput
    toast.set_safe_modes(False, False)
    t.show_message("done")
    assert t.windowFlags() & W.WindowTransparentForInput


def test_toast_with_no_room_beside_the_game_goes_to_the_notice(qapp, monkeypatch):
    fake_game(monkeypatch, QRect(0, 0, 1920, 1080))
    said = []
    toast.set_safe_modes(True, False, said.append)
    t = toast.Toast(None)
    t.show_message("No item found under the cursor")
    assert said == ["No item found under the cursor"] and not t.isVisible()


# --- capture border check ------------------------------------------------------

@pytest.mark.parametrize("ok", [True, False])
def test_border_check_is_logged_and_a_drawn_border_is_a_notice_once(qapp, monkeypatch, caplog, ok):
    monkeypatch.setattr(capture, "capture_border_status", lambda: (ok, "borderless capture access: test"))
    w = capture.CaptureWorker("MapleStory")
    notices = []
    w.info.connect(notices.append)
    with caplog.at_level(logging.INFO, logger="mctracker.capture"):
        w._check_border()
        w._check_border()  # a second attach this run
    assert f"Capture border {'off' if ok else 'may be drawn'}: borderless capture access: test" in caplog.text
    assert notices == ([] if ok else [capture.BORDER_NOTICE])


@pytest.mark.skipif(sys.platform != "win32", reason="WinRT")
def test_border_status_answers_on_this_machine():
    ok, why = capture.capture_border_status()
    assert isinstance(ok, bool) and why


# --- review fixes: title-bar drags, the user's place, the price popup ------------

def test_title_bar_drags_onto_the_game_keep_the_last_clear_place():
    from mctracker.offgame import refuse_move

    game, last = (400, 100, 1600, 900), (10, 10, 310, 210)
    assert refuse_move((300, 200, 600, 400), game, last) == last
    assert refuse_move((20, 950, 320, 1150), game, last) is None  # clear of it: allowed
    assert refuse_move((300, 200, 600, 400), None, last) is None  # no game known


def test_auto_moves_keep_the_users_place_and_never_fight_a_drag(qapp, tmp_path, monkeypatch):
    game = QRect(400, 100, 1200, 800)
    fake_game(monkeypatch, game)
    _tr, o = make(tmp_path)
    o.move(600, 300)
    o.show()
    o.set_safe_window(off_game=True, browser=False)
    assert o.pos() != QPoint(600, 300) and o.user_frame_pos() == QPoint(600, 300)  # what WindowMemory saves
    o.set_safe_window(off_game=False, browser=False)
    assert o.pos() == QPoint(600, 300) and o.user_frame_pos() is None  # back where the user put it
    o.set_safe_window(off_game=True, browser=False)
    o.drag_to(QPoint(20, 900))  # the user's own choice now
    assert o.user_frame_pos() is None
    o._title_drag.moving = True  # a title-bar drag under way
    o.move(600, 300)
    o._keep_off_game()
    assert o.pos() == QPoint(600, 300)
    o._title_drag.moving = False


def test_window_memory_saves_the_users_place_not_the_auto_one(qapp, tmp_path, monkeypatch):
    from mctracker.windowstate import WindowMemory

    fake_game(monkeypatch, QRect(400, 100, 1200, 800))
    tr, o = make(tmp_path)
    mem = WindowMemory(o, tr.settings, "overlay", keep_size=False)
    o.move(600, 300)
    o.show()
    mem.touched = True
    o.set_safe_window(off_game=True, browser=False)
    mem.save()
    assert (tr.settings.windows["overlay"]["x"], tr.settings.windows["overlay"]["y"]) == (600, 300)


def test_browser_style_toggles_never_walk_the_overlay(qapp, tmp_path):
    _tr, o = make(tmp_path)
    o.move(500, 400)
    o.show()
    for browser in (True, False, True, False):
        o.set_safe_window(off_game=False, browser=browser)
        qapp.processEvents()
        assert o.geometry().topLeft() == QPoint(500, 400)  # the title bar comes and goes above the content


def _popup():
    from mctracker.price_popup import PricePopup

    opened = []
    return PricePopup(opened.append, use_web=False), opened


def test_price_popup_opens_beside_the_game_or_in_the_browser(qapp, monkeypatch):
    from mctracker import price_popup

    game = QRect(0, 0, 1200, 1040)  # room for the 560 px popup on its right
    fake_game(monkeypatch, game)
    monkeypatch.setattr(price_popup, "_off_game", True)
    p, opened = _popup()
    p.show_for("Red Potion", cursor=QPoint(700, 500))  # over the game
    assert p.isVisible() and not p.frameGeometry().intersects(game) and opened == []
    p.hide()
    fake_game(monkeypatch, QRect(0, 0, 1920, 1080))  # fullscreen, one monitor
    p.show_for("Blue Potion", cursor=QPoint(700, 500))
    assert not p.isVisible() and opened == ["Blue Potion"]


def test_price_popup_browser_style_bits(qapp, monkeypatch):
    from mctracker import price_popup

    p, _ = _popup()
    monkeypatch.setattr(price_popup, "_browser", True)
    p.show_for("Red Potion", cursor=QPoint(700, 500))
    f = p.windowFlags()
    assert f & W.WindowStaysOnTopHint and (f & W.WindowType_Mask) == W.Window and not f & W.FramelessWindowHint
    monkeypatch.setattr(price_popup, "_browser", False)
    p.show_for("Red Potion", cursor=QPoint(700, 500))
    assert p.windowFlags() == price_popup.POPUP_FLAGS


def test_startup_with_browser_style_saved_keeps_the_restored_place(qapp, tmp_path):
    """app.py's order: Safe mode is applied before WindowMemory restores the overlay, then it shows."""
    from mctracker.windowstate import WindowMemory

    tr, o = make(tmp_path)
    tr.settings.safe_browser_overlay = True
    tr.settings.windows["overlay"] = {"x": 300, "y": 400}
    apply_safe_mode(tr.settings, o)
    WindowMemory(o, tr.settings, "overlay", keep_size=False).restore()
    o.show()
    for _ in range(3):
        qapp.processEvents()
    assert o.frameGeometry().topLeft() == QPoint(300, 400)


def test_turning_both_on_at_once_keeps_the_off_game_move(qapp, tmp_path, monkeypatch):
    game = QRect(400, 100, 1200, 800)
    fake_game(monkeypatch, game)
    _tr, o = make(tmp_path)
    o.move(600, 300)
    o.show()
    o.set_safe_window(off_game=True, browser=True)
    for _ in range(3):
        qapp.processEvents()
    assert not rect(o).intersects(game)
