"""Remembered window positions: save/restore, the off-screen guard, screens with fake geometries. Offscreen."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from mctracker.items import AppSettings  # noqa: E402
from mctracker.windowstate import ScreenInfo, WindowMemory, clean_saved, describe, resolve, title_visible  # noqa: E402

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


def test_clean_saved_drops_junk():
    assert clean_saved({"x": 1, "y": 2, "w": 0, "h": 5, "screen": "A", "sx": 0}) == {"x": 1, "y": 2}
    assert clean_saved({"x": True, "y": 2}) is None and clean_saved([1, 2]) is None
    assert clean_saved({"x": 1, "y": 2, "maximized": "yes"}) == {"x": 1, "y": 2}


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


def test_window_saves_after_a_move_and_comes_back_next_start(app, tmp_path):
    w, st, mem = make(app, tmp_path, "main", [LEFT])
    w.resize(640, 480)
    w.move(300, 200)
    w.show()
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
    w.show()
    app.processEvents()
    for x in range(100, 160, 10):
        w.move(x, 120)
        app.processEvents()
    assert mem._timer.isActive() and "overlay" not in st.windows
    mem._timer.stop()
    mem.save()
    assert "w" not in st.windows["overlay"] and st.windows["overlay"]["x"] == w.pos().x()
    w.close()


def test_restore_ignores_an_off_screen_spot_and_save_never_stores_one(app, tmp_path):
    st = AppSettings(tmp_path / "app.json")
    st.windows["main"] = {"x": 9000, "y": 9000, "w": 500, "h": 400}
    w = QWidget()
    w.resize(111, 222)
    w.move(40, 60)
    WindowMemory(w, st, "main", screens=lambda: [LEFT]).restore()
    assert (w.pos().x(), w.pos().y(), w.width(), w.height()) == (40, 60, 111, 222)  # untouched
    w.move(-32000, -32000)  # where Windows parks a minimized window
    w.show()
    mem = WindowMemory(w, st, "main", screens=lambda: [LEFT])
    mem.save()
    assert st.windows["main"]["x"] == 9000  # the stale value isn't replaced by an off-screen one
    w.close()


def test_maximized_state_is_restored(app, tmp_path):
    st = AppSettings(tmp_path / "app.json")
    st.windows["main"] = {"x": 20, "y": 30, "w": 700, "h": 500, "maximized": True}
    w = QWidget()
    WindowMemory(w, st, "main", screens=lambda: [LEFT]).restore()
    assert w.isMaximized()
    w.show()
    mem = WindowMemory(w, st, "main", screens=lambda: [LEFT])
    mem.save()  # while maximized: the normal-size place stays as it was, the flag stays set
    assert st.windows["main"] == {"x": 20, "y": 30, "w": 700, "h": 500, "maximized": True}
    w.close()
