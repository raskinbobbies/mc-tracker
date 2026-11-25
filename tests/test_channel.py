"""The channel reader: synthetic Change Channel dialogs, the owner's screenshot (if present), and the cache."""

import os
import random
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.demo import DemoFeed  # noqa: E402
from mctracker.items import AppSettings  # noqa: E402
from mctracker.overlay import Overlay  # noqa: E402
from mctracker.session import Tracker  # noqa: E402
from mctracker.vision.channel import StableRead, dialog_maybe_open, read_channel  # noqa: E402

# RGB, from the game's own dialog.
BLUE, GREY, HOVER, BAR, DIALOG = (54, 170, 221), (177, 177, 177), (201, 201, 201), (74, 131, 188), (156, 177, 199)
OWNER_SHOT = Path.home() / ".t3/userdata/attachments/40d7309e-6e1d-4f17-8ef0-18a51d0e821f-aea896bd-c88e-463d-9bfa-150032b737c0.png"


def scene(w=2000, h=1125, seed=3):
    """A noisy game-like backdrop, BGR like the capture."""
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 200, (h // 8, w // 8, 3), dtype=np.uint8)
    return np.ascontiguousarray(np.kron(base, np.ones((8, 8, 1), np.uint8)))


def fill(img, x, y, w, h, rgb):
    img[y:y + h, x:x + w] = rgb[::-1]


def draw_dialog(img, current=26, hover=30, s=1.0, extra_blue=None):
    """The dialog as the game draws it (sizes at s=1: bar 868x36, 103x28 buttons, 109.3/34.25 pitch)."""
    h, w = img.shape[:2]
    left, top = round(w / 2 - 434 * s), round(h * 0.36)
    fill(img, left - 10, top - 40, round(888 * s), round(330 * s), DIALOG)
    fill(img, left, top, round(868 * s), round(36 * s), BAR)
    fill(img, left + round(60 * s), top + round(8 * s), round(70 * s), round(20 * s), (255, 255, 255))  # "WINDIA"
    for n in range(1, 41):
        r, c = divmod(n - 1, 8)
        x, y = left + round((c * 109.3) * s), top + round((60 + r * 34.25) * s)
        colour = BLUE if n in (current, extra_blue) else HOVER if n == hover else GREY
        fill(img, x, y, round(103 * s), round(28 * s), colour)
        fill(img, x + round(35 * s), y + round(8 * s), round(33 * s), round(14 * s), (255, 255, 255))  # label
    return left, top


# --- detector --------------------------------------------------------------------------------

@pytest.mark.parametrize("current", [1, 8, 9, 26, 33, 40])
def test_reads_the_blue_button_whatever_its_cell(current):
    img = scene()
    draw_dialog(img, current=current, hover=7 if current != 7 else 8)
    assert read_channel(img) == current


@pytest.mark.parametrize("size", [(2000, 1125), (1600, 900), (1280, 720), (2560, 1440)])
def test_reads_at_other_window_sizes(size):
    img = scene(*size)
    draw_dialog(img, current=19, hover=5, s=size[0] / 2000)
    assert read_channel(img) == 19


def test_the_hovered_grey_button_is_never_the_current_one():
    img = scene()
    draw_dialog(img, current=26, hover=30)
    assert read_channel(img) == 26  # CH.30 is the lighter grey
    img = scene()
    draw_dialog(img, current=None, hover=30)  # nothing blue: hover alone must read as nothing
    assert read_channel(img) is None


def test_no_dialog_reads_nothing_and_the_precheck_fails_fast():
    img = scene()
    assert not dialog_maybe_open(img)
    assert read_channel(img) is None
    assert read_channel(np.zeros((1125, 2000, 3), np.uint8)) is None
    assert read_channel(np.zeros((10, 10, 3), np.uint8)) is None  # a tiny frame


def test_two_blue_buttons_are_not_trusted():
    img = scene()
    draw_dialog(img, current=26, extra_blue=3)
    assert read_channel(img) is None


@pytest.mark.parametrize("cover", [
    "blue",  # another window over the current button
    "grid_half",  # over half the grid, blue button still showing
    "header",  # over the world bar's end
    "bottom_rows",  # over the lower grid rows
    "left_half",  # cropped by the frame edge
])
def test_a_partly_covered_dialog_reads_nothing_or_the_right_channel(cover):
    img = scene()
    left, top = draw_dialog(img, current=26, hover=30)
    boxes = {
        "blue": (left + 109, top + 160, 110, 36),
        "grid_half": (left + 440, top + 50, 440, 240),
        "header": (left + 600, top - 4, 300, 44),
        "bottom_rows": (left, top + 125, 880, 170),
        "left_half": (0, 0, left + 430, 1125),
    }
    x, y, w, h = boxes[cover]
    fill(img, x, y, w, h, (120, 140, 100))
    got = read_channel(img)
    assert got in (None, 26)  # never a different channel
    if cover in ("blue", "header", "bottom_rows", "left_half"):
        assert got is None


def test_unexpected_colours_do_not_pass_for_buttons():
    img = scene()
    draw_dialog(img, current=26)
    img[:, :, :] = img[:, :, ::-1]  # RGB fed as BGR: the blue becomes orange
    assert read_channel(img) is None


@pytest.mark.skipif(not OWNER_SHOT.exists(), reason="owner screenshot not on this machine")
def test_owner_screenshot_reads_channel_26():
    cv2 = pytest.importorskip("cv2")
    img = cv2.imread(str(OWNER_SHOT))
    assert img is not None and img.shape[:2] == (1125, 2000)
    assert read_channel(img) == 26  # CH.30 is hovered (lighter grey)
    closed = img.copy()
    closed[395:735, 550:1450] = (120, 140, 100)  # blot the dialog out
    assert read_channel(closed) is None


def test_stable_read_needs_two_in_a_row():
    s = StableRead(2)
    assert [s.update(v) for v in (26, 26, 26, None, 26, 12, 12)] == [None, 26, 26, None, None, None, 12]


# --- the cache --------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def tracker(tmp_path):
    return Tracker(save_dir=tmp_path / "sessions", settings=AppSettings(tmp_path / "app.json"))


def test_channel_survives_map_changes_and_new_sessions(tmp_path):
    tr = tracker(tmp_path)
    tr.on_map("Warning Street : The Burnt Land V", ())
    tr.on_channel(26)
    tr.on_map("Warning Street : The Burnt Land VI", (), refine=True)  # a raw read resolving: same visit
    tr.on_map("Henesys : Hunting Ground", ())  # a portal: the channel can't change
    assert tr.channel == 26
    tr.restart()
    assert tr.channel == 26


def test_channel_can_be_cleared_and_replaced(tmp_path):
    tr = tracker(tmp_path)
    tr.on_channel(26)
    tr.on_channel(3)
    assert tr.channel == 3
    tr.on_channel(None)  # a loading transition on the same map (a channel change)
    assert tr.channel is None


def test_capture_sends_every_stable_read_and_clears_on_a_reload():
    from mctracker.capture import CaptureWorker

    cap = CaptureWorker()
    sent = []
    cap.channel_read.connect(sent.append)
    img = scene()
    draw_dialog(img, current=26)
    cap._read_channel(img)
    assert sent == []  # one read isn't enough
    cap._read_channel(img)
    cap._read_channel(img)
    assert sent == [26, 26]  # the tracker drops repeats, so a clear there can never be shadowed here
    cap._read_channel(scene())  # dialog closed: nothing sent
    assert sent == [26, 26]
    cap._channel_reloaded()
    assert sent == [26, 26, None]
    cap._read_channel(img)  # needs two fresh reads again
    assert sent == [26, 26, None]
    cap._read_channel(img)
    assert sent == [26, 26, None, 26]


def test_the_dialog_read_again_after_a_clear_restores_the_pill_channel(tmp_path):
    from mctracker.capture import CaptureWorker

    cap, tr = CaptureWorker(), tracker(tmp_path)
    cap.channel_read.connect(tr.on_channel)
    img = scene()
    draw_dialog(img, current=26)
    cap._read_channel(img)
    cap._read_channel(img)
    assert tr.channel == 26
    cap._channel_reloaded()  # a channel change on the same map
    assert tr.channel is None
    cap._read_channel(img)
    cap._read_channel(img)
    assert tr.channel == 26


# --- display ----------------------------------------------------------------------------------

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


def run_demo(tmp_path, clock, seconds):
    random.seed(7)
    tr = tracker(tmp_path)
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


@pytest.mark.parametrize("seconds", [0, 20, 200])  # waiting, warming up, running
def test_overlay_pill_shows_only_when_known_and_never_changes_the_size(app, tmp_path, clock, seconds):
    tr, ov = run_demo(tmp_path, clock, seconds)
    assert not ov.channel.isVisibleTo(ov)
    size = ov.size()
    tr.on_channel(26)
    ov.refresh()
    QApplication.processEvents()
    assert ov.channel.isVisibleTo(ov) and ov.channel.text() == "Ch. 26" and ov.size() == size
    tr.on_channel(None)
    ov.refresh()
    QApplication.processEvents()
    assert not ov.channel.isVisibleTo(ov) and ov.size() == size


def test_minimap_prompt_beats_the_pill(app, tmp_path, clock):
    tr, ov = run_demo(tmp_path, clock, 200)
    tr.on_channel(26)
    ov.refresh()
    size = ov.size()
    ov.set_minimap_prompt("Open the minimap (top left) so the map can be read")
    ov.refresh()  # the once-a-second refresh keeps the prompt
    QApplication.processEvents()
    assert not ov.channel.isVisibleTo(ov) and ov.size() == size
    ov.set_minimap_prompt(None)
    QApplication.processEvents()
    assert ov.channel.isVisibleTo(ov) and ov.size() == size


def test_main_window_pill_follows_the_channel(app, tmp_path):
    from mctracker.gui import MainWindow

    tr = tracker(tmp_path)
    tr.on_map("Warning Street : The Burnt Land V", ())
    w = MainWindow(tr, Overlay(tr, FlatAssets(tmp_path)), FlatAssets(tmp_path), "alt+a")
    w.show()
    w._refresh_live()
    assert not w.channel_pill.isVisibleTo(w)
    tr.on_channel(26)
    w._refresh_live()
    assert w.channel_pill.isVisibleTo(w) and w.channel_pill.text() == "Ch. 26"
    tr.on_channel(None)
    w._refresh_live()
    assert not w.channel_pill.isVisibleTo(w)


def test_timed_notice_gives_back_the_persistent_one(app, tmp_path):
    from mctracker.gui import MainWindow

    tr = tracker(tmp_path)
    w = MainWindow(tr, Overlay(tr, FlatAssets(tmp_path)), FlatAssets(tmp_path), "alt+a")
    w.show_notice("Settings file was damaged; a backup was kept.")
    w.show_notice("Opened mesos.watch.", timeout_s=20)
    assert w.notice.text() == "Opened mesos.watch."
    w._end_timed_notice()  # what the 20 s timer does
    assert w.notice.text() == "Settings file was damaged; a backup was kept."
    w.show_notice("")
    assert w.notice.isHidden()
