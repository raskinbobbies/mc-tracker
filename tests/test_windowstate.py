"""Remembered window positions: save/restore, the off-screen guard, screens with fake geometries. Offscreen."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mctracker.items import AppSettings, clean_window_entry  # noqa: E402
from mctracker.windowstate import ScreenInfo, WindowMemory, describe, resolve, title_visible  # noqa: E402

LEFT = ScreenInfo("DISPLAY1", QRect(0, 0, 1920, 1040))
RIGHT = ScreenInfo("DISPLAY2", QRect(1920, 0, 1280, 984))
BELOW_LEFT = ScreenInfo("DISPLAY2", QRect(0, 1080, 1280, 984))


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_title_visible_needs_100_by_50_of_the_top_edge_on_one_screen():
    assert title_visible(QRect(100, 100, 800, 600), [LEFT])
    assert title_visible(QRect(1820, 100, 800, 600), [LEFT, RIGHT])  # straddles, 100px on each
    assert not title_visible(QRect(1830, 100, 800, 600), [LEFT])  # only 90px of it on the screen
    assert not title_visible(QRect(100, -40, 800, 600), [LEFT])  # title bar above the screen
    assert not title_visible(QRect(100, 1000, 800, 600), [LEFT])  # top edge under the bottom
    assert not title_visible(QRect(5000, 100, 800, 600), [LEFT])
    assert title_visible(QRect(10, 10, 60, 30), [LEFT])  # a small window only needs to fit itself
    assert not title_visible(QRect(100, 100, 800, 600), [])


def test_resolve_restores_a_visible_spot_and_falls_back_off_screen():
    saved = describe(QRect(200, 150, 1100, 820), [LEFT, RIGHT], "DISPLAY1", maximized=True)
    assert resolve(saved, [LEFT, RIGHT]) == QRect(200, 150, 1100, 820)
    assert resolve(saved, [LEFT]) == QRect(200, 150, 1100, 820)
    on_right = describe(QRect(2100, 80, 900, 700), [LEFT, RIGHT], "DISPLAY2")
    assert resolve(on_right, [LEFT, RIGHT]) == QRect(2100, 80, 900, 700)
    assert resolve(on_right, [LEFT]) is None  # that monitor was unplugged
    assert resolve(None, [LEFT]) is None and resolve({"x": "a", "y": 1}, [LEFT]) is None


def test_resolve_follows_a_screen_that_was_rearranged_and_shrinks_to_fit():
    saved = describe(QRect(2100, 80, 900, 700), [LEFT, RIGHT], "DISPLAY2")  # 180px in from DISPLAY2's left
    assert resolve(saved, [LEFT, BELOW_LEFT]) == QRect(180, 1160, 900, 700)
    small = ScreenInfo("DISPLAY1", QRect(0, 0, 1024, 728))  # lower resolution now
    big = describe(QRect(50, 50, 1100, 820), [LEFT], "DISPLAY1")
    assert resolve(big, [small]) == QRect(50, 50, 1024, 728)
    assert resolve(big, [small], size=(300, 200)) == QRect(50, 50, 300, 200)  # the overlay's own size


def test_clean_window_entry_drops_junk():
    assert clean_window_entry({"x": 1, "y": 2, "w": 0, "h": 5, "screen": "A", "sx": 0}) == {"x": 1, "y": 2}
    assert clean_window_entry({"x": True, "y": 2}) is None and clean_window_entry([1, 2]) is None
    assert clean_window_entry({"x": 1, "y": 2, "maximized": "yes"}) == {"x": 1, "y": 2}


def test_app_settings_round_trip_and_bad_windows_are_ignored(tmp_path):
    p = tmp_path / "app.json"
    st = AppSettings(p)
    st.windows["main"] = describe(QRect(10, 20, 300, 400), [LEFT], "DISPLAY1", maximized=True)
    st.save()
    assert AppSettings(p).windows == st.windows
    p.write_text(json.dumps({"overlay_scale": 1.2, "windows": {"main": {"x": "no"}, "overlay": {"x": 5, "y": 6}, "x": 3}}))
    st2 = AppSettings(p)
    assert st2.windows == {"overlay": {"x": 5, "y": 6}} and st2.overlay_scale == 1.2 and st2.problem is None
    assert not list(tmp_path.glob("app.bad-*.json"))
    p.write_text(json.dumps({"overlay_scale": 1.0, "windows": [1]}))
    assert AppSettings(p).windows == {}


def make(app, tmp_path, key, screens, keep_size=True):
    st = AppSettings(tmp_path / "app.json")
    w = QWidget()
    return w, st, WindowMemory(w, st, key, keep_size=keep_size, screens=lambda: screens)


class FakeScreen:
    """The offscreen platform's screen has no name: stand in for a named monitor."""

    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


def on_screen(w, name):
    w.screen = lambda: FakeScreen(name)


def user_moves(app, w, mem, x, y):
    """Show the window if needed, let startup settle, then move it as the user would."""
    w.show()
    app.processEvents()
    mem.armed = True
    w.move(x, y)
    app.processEvents()


def test_window_saves_after_a_move_and_comes_back_next_start(app, tmp_path):
    w, st, mem = make(app, tmp_path, "main", [LEFT])
    w.resize(640, 480)
    user_moves(app, w, mem, 300, 200)
    mem.save()
    saved = json.loads((tmp_path / "app.json").read_text())["windows"]["main"]
    assert (saved["w"], saved["h"]) == (640, 480)

    w2, st2, mem2 = make(app, tmp_path, "main", [LEFT])
    mem2.restore()
    assert w2.size().width() == 640 and w2.size().height() == 480
    assert w2.pos() == w.pos()
    w.close()


def test_move_is_debounced_to_one_write(app, tmp_path):
    w, st, mem = make(app, tmp_path, "overlay", [LEFT], keep_size=False)
    w.resize(300, 200)
    user_moves(app, w, mem, 100, 120)
    for x in range(110, 160, 10):
        w.move(x, 120)
        app.processEvents()
    assert mem._timer.isActive() and "overlay" not in st.windows
    mem._timer.stop()
    mem.save()
    assert "w" not in st.windows["overlay"] and st.windows["overlay"]["x"] == w.pos().x()
    w.close()


def test_moves_during_startup_are_not_the_users_and_save_nothing(app, tmp_path):
    w, st, mem = make(app, tmp_path, "main", [LEFT])
    w.move(50, 60)
    w.show()
    app.processEvents()  # the system placing the window: not armed yet
    mem.save()
    assert not mem.touched and "main" not in st.windows and not (tmp_path / "app.json").exists()
    w.close()


def test_a_default_place_does_not_overwrite_the_stored_off_screen_spot(app, tmp_path):
    st = AppSettings(tmp_path / "app.json")
    st.windows["main"] = {"x": 9000, "y": 9000, "w": 500, "h": 400}  # that monitor isn't here today
    w = QWidget()
    w.resize(111, 222)
    w.move(40, 60)
    mem = WindowMemory(w, st, "main", screens=lambda: [LEFT])
    mem.restore()
    assert (w.pos().x(), w.pos().y(), w.width(), w.height()) == (40, 60, 111, 222)  # default place
    w.show()
    app.processEvents()
    mem.save()  # e.g. on quit, without the user having touched it
    assert st.windows["main"]["x"] == 9000
    mem.armed = True
    w.move(70, 80)  # the user places it: now the new spot is kept
    app.processEvents()
    mem.save()
    assert st.windows["main"]["x"] == 70
    w.move(-32000, -32000)  # where Windows parks a minimized window: never saved
    app.processEvents()
    mem.save()
    assert st.windows["main"]["x"] == 70
    w.close()


def test_maximized_state_is_restored_and_keeps_the_normal_place(app, tmp_path):
    st = AppSettings(tmp_path / "app.json")
    st.windows["main"] = {"x": 20, "y": 30, "w": 700, "h": 500, "maximized": True}
    st.windows["main"].update(screen="DISPLAY1", sx=0, sy=0)
    w = QWidget()
    on_screen(w, "DISPLAY1")
    mem = WindowMemory(w, st, "main", screens=lambda: [LEFT])
    mem.restore()
    assert w.isMaximized()
    w.show()
    app.processEvents()
    mem.armed = mem.touched = True
    mem.save()  # while maximized: the normal-size place stays as it was, the flag stays set
    assert {k: st.windows["main"][k] for k in ("x", "y", "w", "h", "maximized")} == \
        {"x": 20, "y": 30, "w": 700, "h": 500, "maximized": True}
    w.close()


def test_maximized_on_the_second_monitor_reopens_there():
    saved = describe(QRect(2000, 100, 900, 700), [LEFT, RIGHT], "DISPLAY2", maximized=True)
    assert saved["max_screen"] == "DISPLAY2"
    assert resolve(saved, [LEFT, RIGHT]) == QRect(2000, 100, 900, 700)
    # its normal-size place was on monitor 1 but it was maximized on monitor 2: it goes to monitor 2
    elsewhere = {**describe(QRect(100, 100, 900, 700), [LEFT, RIGHT], "DISPLAY1"), "maximized": True, "max_screen": "DISPLAY2"}
    r = resolve(elsewhere, [LEFT, RIGHT])
    assert r == QRect(1960, 40, 900, 700)
    # monitor 2 unplugged: back to the normal-size place
    assert resolve(elsewhere, [LEFT]) == QRect(100, 100, 900, 700)


def test_save_while_maximized_records_the_screen_it_is_on(app, tmp_path):
    st = AppSettings(tmp_path / "app.json")
    st.windows["main"] = {"x": 20, "y": 30, "w": 700, "h": 500, "screen": "DISPLAY1", "sx": 0, "sy": 0}
    w = QWidget()
    w.resize(700, 500)
    w.move(20, 30)
    on_screen(w, "DISPLAY2")
    mem = WindowMemory(w, st, "main", screens=lambda: [LEFT, ScreenInfo("DISPLAY2", QRect(0, 0, 800, 800))])
    w.showMaximized()
    app.processEvents()
    mem.armed = mem.touched = True
    mem.save()
    e = st.windows["main"]
    assert e["maximized"] is True and e["max_screen"] == "DISPLAY2" and e["screen"] == "DISPLAY2"
    w.close()


def test_maximized_with_no_screen_to_restore_to_still_maximizes(app, tmp_path):
    st = AppSettings(tmp_path / "app.json")
    st.windows["main"] = {"x": 9000, "y": 9000, "w": 700, "h": 500, "screen": "GONE", "sx": 8000, "sy": 8000,
                          "maximized": True, "max_screen": "GONE"}
    assert resolve(st.windows["main"], [LEFT]) is None
    w = QWidget()
    w.move(40, 60)
    WindowMemory(w, st, "main", screens=lambda: [LEFT]).restore()
    assert w.isMaximized() and (w.pos().x(), w.pos().y()) == (40, 60)  # not moved: default screen
    w.close()


def test_overlay_only_needs_a_grabbable_part_visible_anywhere():
    cut = QRect(500, -70, 300, 120)  # 50px of it show at the top of the screen
    assert not title_visible(cut, [LEFT])  # a normal window's title bar would be unreachable
    assert title_visible(cut, [LEFT], whole=True)
    assert not title_visible(QRect(500, -80, 300, 120), [LEFT], whole=True)  # 40px: too little
    assert title_visible(QRect(500, 1005, 300, 30), [LEFT], whole=True)  # a short one just has to fit
    assert not title_visible(QRect(500, 1020, 300, 30), [LEFT], whole=True)
    assert title_visible(QRect(1880, 400, 300, 120), [LEFT, RIGHT], whole=True)  # 40px + 260px: the right screen
    saved = describe(cut, [LEFT], "DISPLAY1")
    assert resolve(saved, [LEFT], size=(300, 120), whole=True) == cut
    assert resolve(saved, [LEFT], size=(300, 120)) is None


def test_overlay_cut_by_the_screen_edge_is_saved(app, tmp_path):
    w, st, mem = make(app, tmp_path, "overlay", [LEFT], keep_size=False)
    w.resize(300, 120)
    user_moves(app, w, mem, 500, -60)
    mem.save()
    assert st.windows["overlay"]["y"] == w.pos().y() < 0


def test_screen_origin_uses_the_whole_screen_not_the_taskbar_area():
    with_bar = ScreenInfo("DISPLAY1", QRect(0, 0, 1920, 1040), QRect(0, 0, 1920, 1080))
    bar_on_top = ScreenInfo("DISPLAY1", QRect(0, 40, 1920, 1040), QRect(0, 0, 1920, 1080))
    saved = describe(QRect(200, 150, 800, 600), [with_bar], "DISPLAY1")
    assert (saved["sx"], saved["sy"]) == (0, 0)
    assert resolve(saved, [bar_on_top]) == QRect(200, 150, 800, 600)  # the taskbar moved: the window didn't
