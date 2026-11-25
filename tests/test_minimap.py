"""Minimap title crop: fits the panel (whatever its width) and keeps the text."""

import numpy as np
import pytest

from mctracker.vision import reader as R

W, H = 2560, 1440
FILL = (204, 187, 153)  # BGR title fill
SCENE = (10, 200, 10)   # a bright green game scene
TEXT = (250, 250, 250)


def _frame(panel_right: float, text_left: float = 0.0435, text_right: float | None = None):
    """Scene with a minimap title panel from x=0.006 to ``panel_right``, a dark
    icon block, and white text bars on both title lines."""
    f = np.empty((H, W, 3), np.uint8)
    f[:] = SCENE
    f[int(0.01 * H):int(0.10 * H), int(0.006 * W):int(panel_right * W)] = FILL
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
    assert x1 >= 0.167  # widest measured panel
