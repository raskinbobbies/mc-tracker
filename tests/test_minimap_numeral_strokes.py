"""The map name's numeral: the title line is found wherever it sits, and I/II/III are counted from the bars."""

import numpy as np
import pytest

from mctracker import gamedata
from mctracker.vision import reader as R

W, H = 2560, 1440
FILL = (204, 187, 153)
WHITE = (250, 250, 250)


def _line(numeral_bars: int | None, word: bool = True, glyph_h: int = 18) -> np.ndarray:
    """A name-line crop: a word (a wide block with a gap), then ``numeral_bars`` 4 px bars 2 px apart."""
    img = np.empty((33, 300, 3), np.uint8)
    img[:] = FILL
    if word:
        for x in range(100, 180, 12):
            img[8:8 + 14, x:x + 9] = WHITE  # lowercase letters
        img[8 - 4:8 + 14, 183:187] = WHITE  # an ascender ("d"): full height, but part of the word
    x = 202
    for _ in range(numeral_bars or 0):
        img[4:4 + glyph_h, x:x + 4] = WHITE
        x += 6
    return img


@pytest.mark.parametrize("bars", [1, 2, 3])
def test_counts_numeral_bars(bars):
    assert R.numeral_strokes(_line(bars)) == bars


def test_a_word_ending_in_a_tall_letter_is_not_a_numeral():
    assert R.numeral_strokes(_line(None)) is None


def test_a_line_that_is_only_bars_is_not_a_numeral():
    assert R.numeral_strokes(_line(2, word=False)) is None


def test_short_bars_are_letters_not_a_numeral():
    img = _line(None)
    img[8:22, 202:206] = WHITE  # an "i"-height stroke after a word gap
    assert R.numeral_strokes(img) is None


@pytest.mark.parametrize("read, n, want", [
    ("Forgotten Entrance in", 2, "Forgotten Entrance II"),
    ('Forgotten Entrance "', 2, "Forgotten Entrance II"),
    ("Forgotten Entrance ", 2, "Forgotten Entrance II"),
    ("Forgotten Entrance I", 2, "Forgotten Entrance II"),  # a two-stroke numeral read as one
    ("Forgotten Entrance ll", 3, "Forgotten Entrance III"),
    ("Forgotten Entrance II", 2, "Forgotten Entrance II"),
    ("The Burnt Land V", 1, "The Burnt Land V"),  # not I-only: left alone
    ("Primeval Foresti", 1, "Primeval Foresti"),  # a glued tail: match_map's job
    ("Forgotten Hollow", 1, "Forgotten Hollow"),
])
def test_with_strokes(read, n, want):
    assert gamedata.with_strokes(read, n) == want


def _frame(shift_px: int) -> np.ndarray:
    """A minimap title panel whose two text lines sit ``shift_px`` above their usual rows."""
    f = np.empty((H, W, 3), np.uint8)
    f[:] = (10, 200, 10)
    f[int(0.01 * H):int(0.10 * H), int(0.006 * W):int(0.15 * W)] = FILL
    f[int(0.043 * H):int(0.089 * H), int(0.008 * W):int(0.038 * W)] = (60, 20, 20)  # icon
    for y0, y1 in ((0.047, 0.060), (0.070, 0.083)):
        a, b = int(y0 * H) - shift_px, int(y1 * H) - shift_px
        f[a:b, int(0.0435 * W):int(0.14 * W):3] = WHITE
    return f


def _ink_rows(img):
    return np.flatnonzero((img.min(axis=2) >= 240).any(axis=1))


def test_a_raised_title_is_cropped_whole():
    f = _frame(10)  # ~10 px higher, as on the owner's Forgotten Entrance frames
    _, name, _, found = R.minimap_title(f)
    rows = _ink_rows(name)
    assert found and rows.size == int(0.083 * H) - int(0.070 * H)  # every text row is in
    assert rows[0] > 0 and rows[-1] < name.shape[0] - 1  # with margin above and below


def test_title_at_its_usual_rows_keeps_the_static_crop():
    f = _frame(0)
    span = R.minimap_span(f)
    assert R._title_rows(f, span) is None
