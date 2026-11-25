"""Overlay scale: grows/shrinks as one unit, persists, and nothing overlaps or clips. Offscreen."""

import os
import random
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.path.isdir("C:/Windows/Fonts"):  # offscreen finds no fonts by itself: use real text metrics
    os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent, QPixmap, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from mctracker.demo import DemoFeed  # noqa: E402
from mctracker.items import AppSettings  # noqa: E402
from mctracker.overlay import TILE, Overlay  # noqa: E402
from mctracker.session import Tracker  # noqa: E402


class FakeAssets:
    """Serves a flat 40x40 square for every sprite (no network)."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "sprite.png"
        pm = QPixmap(40, 40)
        pm.fill(QColor("#4a90d9"))
        pm.save(str(self.path))

    def mob_sprite(self, _id, cb):
        cb(str(self.path))

    item_icon = mob_sprite


CLOCK = [0.0]  # seconds the demo feed has run ahead of the real clock


@pytest.fixture(autouse=True)
def demo_clock(monkeypatch):
    """The feed ticks once a second; party kills settle seconds after their lines."""
    real = time.monotonic
    CLOCK[0] = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: real() + CLOCK[0])


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def make(tmp_path, settings=None, demo=True):
    tr = Tracker(save_dir=tmp_path / "sessions", settings=settings or AppSettings())
    feed = None
    if demo:
        random.seed(3)
        feed = DemoFeed(tr)
        feed.timer.stop()
        for _ in range(40):
            feed.tick()
            CLOCK[0] += 1.0
    ov = Overlay(tr, FakeAssets(tmp_path))
    ov.timer.stop()
    ov.show()
    ov.refresh()
    return tr, feed, ov


def ctrl_wheel(ov, delta, target=None):
    w = target or ov
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, delta),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(w, ev)


def test_size_grows_at_150_and_returns_at_100(app, tmp_path):
    _, _, ov = make(tmp_path)
    base = ov.size()
    ov.set_scale(1.5)
    assert ov.size().width() > base.width() * 1.3 and ov.size().height() > base.height() * 1.3
    ov.set_scale(1.0)
    assert abs(ov.size().width() - base.width()) < 20 and ov.size().height() == base.height()  # live text may differ a digit
    ov.set_scale(5)  # clamped to 200%
    assert ov.scale == 2.0
    ov.set_scale(0.1)  # clamped to 60%
    assert ov.scale == 0.6


def test_everything_scales_together(app, tmp_path):
    _, _, ov = make(tmp_path)
    tile = next(iter(ov.kills.tiles.values()))
    assert tile.img.width() == TILE.width()
    ov.set_scale(2.0)
    assert tile.img.width() == TILE.width() * 2 and tile.img.height() == TILE.height() * 2
    assert tile.img.pixmap().width() == 64  # the 40px sprite fits the 32px tile at 100%, so 2x that
    assert "font-size: 60px" in ov.frame.styleSheet()  # the 30px headline
    assert ov.lay.contentsMargins().left() == 24
    assert ov.grip.width() == 28
    ov.set_scale(0.6)
    assert tile.img.width() == round(TILE.width() * 0.6)


def test_scale_persists_in_settings(app, tmp_path):
    path = tmp_path / "app.json"
    _, _, ov = make(tmp_path, AppSettings(path))
    ov.set_scale(1.5)
    assert AppSettings(path).overlay_scale == 1.5
    _, _, ov2 = make(tmp_path, AppSettings(path), demo=False)  # "restart"
    assert ov2.scale == 1.5
    assert not AppSettings(path).problem


def test_bad_saved_scale_falls_back_to_100(app, tmp_path):
    path = tmp_path / "app.json"
    path.write_text('{"overlay_scale": 9}')
    st = AppSettings(path)
    assert st.overlay_scale == 1.0 and st.problem and list(tmp_path.glob("app.bad-*.json"))


def test_ctrl_wheel_steps_10_percent_and_plain_wheel_does_nothing(app, tmp_path):
    _, _, ov = make(tmp_path)
    ctrl_wheel(ov, 120)
    assert ov.scale == 1.1
    ctrl_wheel(ov, 120)
    assert ov.scale == 1.2
    ctrl_wheel(ov, -120)
    ctrl_wheel(ov, -120)
    assert ov.scale == 1.0
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(ov, ev)
    assert ov.scale == 1.0


def test_grip_drag_scales_and_double_click_resets(app, tmp_path):
    _, _, ov = make(tmp_path)
    grip = ov.grip
    centre = grip.rect().center()
    QTest.mousePress(grip, Qt.MouseButton.LeftButton, pos=centre)
    # Qt's mouse-move helper moves the cursor; drive the grip directly with the global delta instead.
    start = grip.mapToGlobal(centre)
    far = start + QPoint(ov.width() // 2, ov.height() // 2)  # +50% each way
    ev = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(centre), QPointF(far), Qt.MouseButton.NoButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    grip.mouseMoveEvent(ev)
    assert 1.4 < ov.scale < 1.6
    QTest.mouseRelease(grip, Qt.MouseButton.LeftButton, pos=centre)
    pos = ov.pos()
    assert ov._drag is None and ov.pos() == pos  # dragging the grip didn't move the window
    QTest.mouseDClick(grip, Qt.MouseButton.LeftButton, pos=centre)
    assert ov.scale == 1.0


def test_left_drag_still_moves_and_opacity_works(app, tmp_path):
    _, _, ov = make(tmp_path)
    ov.move(100, 100)
    QTest.mousePress(ov, Qt.MouseButton.LeftButton, pos=QPoint(3, 3))
    glob = QPointF(ov.mapToGlobal(QPoint(3, 3))) + QPointF(40, 25)
    ov.mouseMoveEvent(QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(3, 3), glob, Qt.MouseButton.NoButton,
                                  Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, pos=QPoint(3, 3))
    assert ov.pos() == QPoint(140, 125)
    ov.set_opacity(50)
    assert abs(ov.windowOpacity() - 0.5) < 0.01
    ov.set_opacity(5)  # never fully see-through
    assert abs(ov.windowOpacity() - 0.3) < 0.01


@pytest.mark.parametrize("scale", [0.6, 1.0, 2.0])
def test_no_overlap_or_clipping(app, tmp_path, scale):
    """Every label/tile is inside the frame, tiles don't overlap, no text is cut off."""
    tr, feed, ov = make(tmp_path)
    ov.set_scale(scale)
    app.processEvents()
    frame = ov.frame.rect()
    assert ov.frame.size() == ov.size()

    def box(w):
        return w.rect().translated(w.mapTo(ov.frame, QPoint(0, 0)))

    for lbl in ov.findChildren(QLabel):
        if lbl.isVisibleTo(ov) and lbl.text() and lbl is not ov.map:  # the map name elides instead of widening
            assert frame.contains(box(lbl)), (lbl.text(), box(lbl), frame)
            hint = lbl.sizeHint()
            assert lbl.width() >= hint.width() - 1 and lbl.height() >= hint.height() - 1, (lbl.text(), lbl.size(), hint)
    assert frame.contains(box(ov.map))
    for row in ov.rows:
        tiles = [t for t in row.tiles.values()]
        assert tiles
        rects = [box(t) for t in tiles]
        for i, a in enumerate(rects):
            assert frame.contains(a)
            assert all(not a.intersects(b) for b in rects[i + 1:])
        for t in tiles:  # the count badge sits inside its tile
            assert t.rect().contains(t.count.geometry()), (t.count.text(), t.count.geometry(), t.rect())
    for w in (ov.panel, ov.items, ov.kills):
        if w.isVisibleTo(ov):
            assert not box(ov.grip).intersects(box(w)), w
    assert frame.contains(box(ov.grip))
    assert not box(ov.panel).intersects(box(ov.left_w))
    if os.environ.get("MC_SHOT_DIR"):  # manual look: MC_SHOT_DIR=... pytest tests/test_overlay_scale.py
        ov.grab().save(os.path.join(os.environ["MC_SHOT_DIR"], f"overlay-{int(scale * 100)}.png"))


def test_small_wheel_deltas_add_up_to_one_step_and_save_once(app, tmp_path):
    path = tmp_path / "app.json"
    _, _, ov = make(tmp_path, AppSettings(path))
    for _ in range(4):  # a touchpad: four +30 events make one 120 notch
        ctrl_wheel(ov, 30)
    assert ov.scale == 1.1
    ctrl_wheel(ov, 30)  # 30 left over, still short of a step
    assert ov.scale == 1.1
    ctrl_wheel(ov, 240)  # two full notches at once
    assert ov.scale == 1.3
    assert not path.exists() and ov._save_timer.isActive()  # nothing written per event
    writes = []
    save = ov.tracker.settings.save
    ov.tracker.settings.save = lambda: (writes.append(ov.scale), save())
    ov._save_timer.timeout.emit()  # the pause after the wheel stops
    assert writes == [1.3] and AppSettings(path).overlay_scale == 1.3


def test_pending_wheel_save_is_flushed_on_close(app, tmp_path):
    path = tmp_path / "app.json"
    _, _, ov = make(tmp_path, AppSettings(path))
    ctrl_wheel(ov, 120)
    ov.close()
    assert AppSettings(path).overlay_scale == 1.1


def test_grip_drag_lays_out_once_per_interval_and_on_release(app, tmp_path):
    _, _, ov = make(tmp_path)
    laid_out = []
    apply = ov._apply_scale
    ov._apply_scale = lambda: (laid_out.append(ov._applied), apply())
    grip = ov.grip
    centre = grip.rect().center()
    QTest.mousePress(grip, Qt.MouseButton.LeftButton, pos=centre)
    start = grip.mapToGlobal(centre)
    for i in range(1, 31):  # a 30-step drag
        ev = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(centre), QPointF(start + QPoint(i * 4, i * 2)),
                         Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        grip.mouseMoveEvent(ev)
    assert ov.scale > 1.2 and laid_out == []  # target follows the mouse, no layout yet
    ov._apply_timer.timeout.emit()  # one interval passes
    assert len(laid_out) == 1
    QTest.mouseRelease(grip, Qt.MouseButton.LeftButton, pos=centre)
    assert ov._applied == ov.scale and not ov._apply_timer.isActive()


def test_rescale_keeps_cached_image_and_gif_keeps_playing(app, tmp_path):
    from mctracker.overlay import Tile

    png = FakeAssets(tmp_path).path
    tile = Tile("t")
    tile.set_image(str(png))
    png.unlink()  # a rescale must not read the file again
    tile.set_scale(2.0)
    assert tile.img.pixmap().width() == 64
    tile.set_scale(0.6)
    assert tile.img.pixmap().width() == round(32 * 0.6)

    gif = tmp_path / "anim.gif"
    pm = QPixmap(40, 40)
    pm.fill(QColor("#d94a4a"))
    pm.save(str(gif), "PNG")  # QMovie sniffs the content, so a one-frame image is enough
    tile.set_image(str(gif))
    movie = tile._movie
    assert movie is not None
    tile.set_scale(1.5)
    tile.set_scale(2.0)
    assert tile._movie is movie and movie.scaledSize().width() == 64
