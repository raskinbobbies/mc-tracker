"""Read the current channel from the in-game Change Channel dialog.

The HUD never shows the channel; only that dialog does. It is a centred window with a blue
world-name bar over a grid of CH.1 to CH.40 buttons (8 per row). The current channel's button is
filled bright blue and a hovered one is a lighter grey. We find the world bar, lay the 8x5 grid
off it, and map the one blue cell to its number. No OCR: the position is the answer.

``read_channel`` returns None unless it is sure: no dialog, a covered or cropped one, no blue
cell, more than one, or too few grey cells to trust the layout. Showing nothing beats a wrong
channel. When the dialog is closed it costs a strided sample of a few thousand pixels.
"""

from __future__ import annotations

import numpy as np

COLS, ROWS = 8, 5
BAR_RGB = (74, 131, 188)  # the world-name bar
BAR_TOL = 24  # summed per-channel difference
BAR_ASPECT = 868 / 36  # bar width / height, as the strict colour mask sees it
ASPECT_TOL = 0.15
BAR_FILL_MIN = 0.7  # the world name and logo cover the rest of the bar
SCALE_RANGE = (0.4, 2.5)  # bar width relative to its 868 px at the reference size
# Layout at the reference size (bar 868 px wide), from the bar's top-left corner.
CELL_X0, CELL_DX, CELL_W = 0.0, 109.3, 103
CELL_Y0, CELL_DY = 60, 34.25
BAND_Y, BAND_H = 2, 4  # the text-free strip at the top of a button
MARGIN_X = 4
MIN_GREY_CELLS = 30  # of the 39 buttons that aren't current; a cursor or window may hide a few
CELL_PURITY = 0.9  # share of a cell's strip that must be the cell's colour

# Strided pre-check window (fractions of the frame): where the bar can be on a centred dialog.
PRE_Y = (0.30, 0.55)
PRE_X = (0.20, 0.80)
PRE_STEP = 4
PRE_MIN = 0.01  # share of sampled pixels that must be bar-coloured


def _is_bar(px: np.ndarray) -> np.ndarray:
    d = np.abs(px.astype(np.int16) - np.array(BAR_RGB, np.int16))
    return d.sum(-1) < BAR_TOL


def _region(frame: np.ndarray) -> tuple[int, int, int, int]:
    h, w = frame.shape[:2]
    return int(h * PRE_Y[0]), int(h * PRE_Y[1]), int(w * PRE_X[0]), int(w * PRE_X[1])


def dialog_maybe_open(frame: np.ndarray) -> bool:
    """Cheap pre-check: is there enough of the world bar's colour where the dialog sits?"""
    y0, y1, x0, x1 = _region(frame)
    return float(_is_bar(frame[y0:y1:PRE_STEP, x0:x1:PRE_STEP]).mean()) >= PRE_MIN


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) of each run of True, end exclusive."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist()))


def find_bar(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) of the world-name bar, or None if it isn't one whole, bar-shaped block."""
    y0, y1, x0, x1 = _region(frame)
    mask = _is_bar(frame[y0:y1, x0:x1])
    rows = mask.sum(1)
    if rows.max(initial=0) < 100:
        return None
    ys = _runs(rows >= rows.max() * 0.5)
    ya, yb = max(ys, key=lambda r: rows[r[0]:r[1]].sum())
    cols = mask[ya:yb].sum(0)
    # The widest full-height stretch, then any short ones just left of it: the logo and world
    # name break the bar's left end.
    runs = _runs(cols >= (yb - ya) * 0.8)
    if not runs:
        return None
    xa, xb = max(runs, key=lambda r: r[1] - r[0])
    reach = (xb - xa) * 0.25
    xa = min([xa] + [a for a, b in runs if b - a >= 8 and xa - b <= reach and a < xa])
    w, h = xb - xa, yb - ya
    if not (SCALE_RANGE[0] <= w / 868 <= SCALE_RANGE[1]) or h < 4:
        return None
    if abs(w / h / BAR_ASPECT - 1) > ASPECT_TOL:
        return None
    if mask[ya:yb, xa:xb].mean() < BAR_FILL_MIN:
        return None
    return x0 + xa, y0 + ya, w, h


def _cell_colour(strip: np.ndarray) -> str:
    """"blue" (the current channel), "grey" (idle or hovered), or "" (anything else)."""
    px = strip.reshape(-1, 3).astype(np.int16)
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    spread = px.max(1) - px.min(1)
    mean = px.mean(1)
    blue = (r < 110) & (b > 170) & (b - r > 110) & (g > 110) & (g < b)
    grey = (spread < 14) & (mean >= 150) & (mean <= 225)
    if blue.mean() >= CELL_PURITY:
        return "blue"
    return "grey" if grey.mean() >= CELL_PURITY else ""


def read_channel(bgr: np.ndarray) -> int | None:
    """The current channel (1 to 40) from a BGR frame (as captured), or None unless certain."""
    if bgr.ndim != 3 or bgr.shape[2] < 3:
        return None
    frame = bgr[:, :, 2::-1]  # RGB view, no copy
    if not dialog_maybe_open(frame):
        return None
    bar = find_bar(frame)
    if bar is None:
        return None
    bx, by, bw, _bh = bar
    s = bw / 868
    h, w = frame.shape[:2]
    blue: list[int] = []
    grey = 0
    for i in range(COLS * ROWS):
        r, c = divmod(i, COLS)
        x = round(bx + (CELL_X0 + c * CELL_DX) * s) + round(MARGIN_X * s)
        x_end = round(bx + (CELL_X0 + c * CELL_DX + CELL_W) * s) - round(MARGIN_X * s)
        y = round(by + (CELL_Y0 + r * CELL_DY + BAND_Y) * s)
        y_end = y + max(1, round(BAND_H * s))
        if x_end > w or y_end > h:
            return None  # the grid runs off the frame: not the whole dialog
        kind = _cell_colour(frame[y:y_end, x:x_end])
        if kind == "blue":
            blue.append(i + 1)
        elif kind == "grey":
            grey += 1
    if len(blue) != 1 or grey < MIN_GREY_CELLS:
        return None
    return blue[0]


class StableRead:
    """Passes a value on only after ``need`` identical reads in a row; None resets."""

    def __init__(self, need: int = 2):
        self.need = need
        self._value: int | None = None
        self._n = 0

    def update(self, value: int | None) -> int | None:
        if value is None or value != self._value:
            self._value, self._n = value, 0 if value is None else 1
        else:
            self._n += 1
        return self._value if self._value is not None and self._n >= self.need else None
