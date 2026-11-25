"""Minimap title crop: fits the panel (whatever its width) and keeps the text."""

import numpy as np
import pytest

from mctracker.vision import reader as R

W, H = 2560, 1440
FILL = (204, 187, 153)  # BGR title fill
SCENE = (10, 200, 10)   # a bright green game scene
TEXT = (250, 250, 250)


BORDER = (153, 119, 85)  # BGR panel frame line
SKY = (206, 186, 152)  # a scene colour within the fill tolerance


def _frame(panel_right: float, text_left: float = 0.0435, text_right: float | None = None,
           border: bool = False, scene=SCENE):
    """Scene with a minimap title panel from x=0.006 to ``panel_right``, a dark
    icon block, and white text bars on both title lines. ``border``: a 5 px frame
    line from ``panel_right`` on, as in the game."""
    f = np.empty((H, W, 3), np.uint8)
    f[:] = scene
    f[int(0.01 * H):int(0.10 * H), int(0.006 * W):int(panel_right * W)] = FILL
    if border:
        x = int(panel_right * W)
        f[int(0.01 * H):int(0.10 * H), x:x + 5] = BORDER
    f[int(0.043 * H):int(0.089 * H), int(0.008 * W):int(0.038 * W)] = (60, 20, 20)  # icon
    tr = text_right if text_right is not None else panel_right - 0.006
    for y0, y1 in ((0.047, 0.060), (0.070, 0.083)):
        f[int(y0 * H):int(y1 * H), int(text_left * W):int(tr * W):3] = TEXT  # dashed: glyph-like gaps
    return f


@pytest.mark.parametrize("right", [0.123, 0.138, 0.167])
def test_span_matches_panel_width(right):
    x0, x1 = R.minimap_span(_frame(right))
    assert x0 == pytest.approx(0.039, abs=0.004)  # just right of the icon
    assert x1 == pytest.approx(right, abs=0.002)


@pytest.mark.parametrize("right", [0.123, 0.167])
def test_crop_has_leftmost_text_and_no_scene(right):
    street, name = R.minimap_crops(_frame(right, text_left=0.0435))
    for c in (street, name):
        # The first text column is inside the crop, near its left edge...
        bright = np.flatnonzero((c.min(axis=2) >= 240).any(axis=0))
        assert bright.size and bright[0] < 0.012 * W
        # ...and not a single scene pixel is: it stops at the panel's edge.
        assert not (c == np.array(SCENE, np.uint8)).all(axis=2).any()


def test_no_panel_falls_back_to_static_regions():
    f = np.zeros((H, W, 3), np.uint8)
    assert R.minimap_span(f) is None
    street, name = R.minimap_crops(f)
    assert street.shape == R.crop(f, "minimap_street").shape
    assert name.shape == R.crop(f, "minimap_name").shape


def test_static_region_clears_icon_and_covers_widest_panel():
    x0, _, x1, _ = R.REGIONS["minimap_name"]
    assert x0 > 0.039  # icon ends at 0.039
    assert x1 >= 0.197  # widest measured panel: "Tree Dungeon, Forest Up North VII"


@pytest.mark.parametrize("right", [0.197, 0.215, 0.26])
def test_wide_panel_is_not_cut_short(right):
    # #26: the VII panel ends at 0.197, past the old 0.170 fallback and near the
    # old 0.200 search limit; wider ones must not be capped either.
    f = _frame(right, border=True)
    x0, x1 = R.minimap_span(f)
    assert x1 == pytest.approx(right, abs=0.001)
    _, name, clipped, _ = R.minimap_title(f)
    assert not clipped
    bright = np.flatnonzero((name.min(axis=2) >= 240).any(axis=0))
    pad = round(0.006 * W)
    assert bright[-1] >= pad + int((right - 0.006 - x0) * W) - 4  # the last glyph is in


@pytest.mark.parametrize("gap_px", [1, 2])
def test_crop_runs_to_the_border_not_the_last_fill(gap_px):
    # A last glyph almost touching the border: the span ends at the border column,
    # not before the glyph, and the read is flagged as possibly clipped.
    right = 0.16
    f = _frame(right, text_right=right - gap_px / W, border=True)
    # A solid "I" ending each line: only the fill between the lines is left in
    # those columns (~10%, under the 25% a column needs to count as panel).
    for y0, y1 in ((0.047, 0.064), (0.068, 0.086)):
        f[int(y0 * H):int(y1 * H), int(right * W) - 6:int(right * W) - gap_px] = TEXT
    x0, x1 = R.minimap_span(f)
    assert x1 == pytest.approx(right, abs=1 / W)
    assert R.minimap_title(f)[2]


def test_sky_past_the_border_is_not_bridged():
    # #18: a fill-coloured sky (with dark objects) right of the panel's 5 px border.
    f = _frame(0.1235, border=True, scene=SKY)
    f[int(0.05 * H):int(0.08 * H), int(0.15 * W):int(0.155 * W)] = (20, 20, 20)
    x0, x1 = R.minimap_span(f)
    assert x1 == pytest.approx(0.1235, abs=0.001)
    street, name, clipped, _ = R.minimap_title(f)
    assert not clipped
    assert not (name == np.array((20, 20, 20), np.uint8)).all(axis=2).any()


def test_static_fallback_is_never_flagged_clipped():
    f = np.zeros((H, W, 3), np.uint8)
    assert R.minimap_title(f)[2] is False


class _FakeOCR(R.Reader):
    """A Reader whose line OCR returns canned text: street, then map name."""

    def __init__(self, street: str, name: str):
        self.reads = [street, name]

    def _line(self, img):
        return self.reads.pop(0)


@pytest.mark.parametrize("frame, street, name, status", [
    (_frame(0.167), "Victoria Road", "Tree Dungeon, Forest Up North VII", "ok"),
    (_frame(0.167), "Victoria Road", "  ", "names_hidden"),
    (_frame(0.167), "", "", "names_hidden"),
    (np.zeros((H, W, 3), np.uint8), "", "", "missing"),  # moved or closed
    (np.full((H, W, 3), SCENE, np.uint8), "junk", "junk", "missing"),
])
def test_minimap_status(frame, street, name, status):
    s = R.GameState()
    _FakeOCR(street, name).read_map(frame, s)
    assert s.minimap_status == status
