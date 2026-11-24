"""Read item stack counts (and icon fingerprints) from the quickslot panel.

The panel sits bottom-right: 8 slots in 2 rows x 4. Each item slot shows the
item's icon with its stack count drawn over the bottom-left corner in the
game's bitmap number font: a white-to-light-blue fill with a pure black
outline. Generic OCR reads that badly straight off the screen (white digits on
white slot backgrounds, ~40% correct), so we first isolate the digit *fill*:
light pixels whose whole surrounding ring is black outline. That gives a clean
black-on-white rendering of just the digits, which rec-only OCR reads reliably.

Geometry was measured on 2560x1440 fullscreen with UI Size = Ideal Ratio (UI
art drawn at 2x). Other sizes are scaled from that and are unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import cv2
import numpy as np

# Panel bounding box as fractions of the client area (2560x1440: 1785,1152 - 2077,1298).
PANEL = (1785 / 2560, 1152 / 1440, 2077 / 2560, 1298 / 1440)
PANEL_PX = (292, 146)  # panel size at the reference resolution
PITCH = 70  # slot pitch (both axes), reference px
COLS, ROWS = 4, 2
# Within a slot cell (origin = top-left of cell c,r at (PITCH*c, PITCH*r)):
COUNT_BOX = (4, 44, 70, 76)  # where the stack count is drawn
ICON_BOX = (14, 28, 66, 46)  # icon middle band: below the key label, above the count

DARK_MAX = 60  # outline: every channel below this
MIN_RING_DARK = 0.8  # fraction of a fill component's border that must be outline
MIN_PART_AREA = 4  # one art pixel at 2x
DIGIT_H = 22  # glyph height incl. outline, reference px


def panel(frame: np.ndarray) -> np.ndarray:
    """Panel crop resampled to reference size (identity at 2560x1440)."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = PANEL
    img = frame[round(y0 * h): round(y1 * h), round(x0 * w): round(x1 * w)]
    if img.ndim == 3 and img.shape[2] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    if img.size and (img.shape[1], img.shape[0]) != PANEL_PX:
        shrink = img.shape[1] > PANEL_PX[0]
        img = cv2.resize(img, PANEL_PX, interpolation=cv2.INTER_AREA if shrink else cv2.INTER_CUBIC)
    return img


def _cell(p: np.ndarray, slot: int, box: tuple[int, int, int, int]) -> np.ndarray:
    r, c = divmod(slot, COLS)
    x0, y0, x1, y1 = box
    ox, oy = PITCH * c, PITCH * r
    return p[oy + y0: oy + y1, ox + x0: ox + x1]


_K4 = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], np.uint8)


def digit_mask(crop: np.ndarray) -> np.ndarray:
    """255 where a pixel belongs to the fill of a count digit, else 0."""
    b, g, r = (crop[..., k].astype(np.int16) for k in range(3))
    dark = np.maximum(np.maximum(b, g), r) < DARK_MAX
    # Number fill runs from white at the top to light blue (153,204,255) at the bottom.
    fill = (b > 200) & (g > 170) & (r > 120) & (b >= r - 10)
    # The outline also runs *through* glyphs (a "3" is several separate fill
    # pieces), so a digit is any number of enclosed pieces, not one component.
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fill.astype(np.uint8), connectivity=4)
    H, W = fill.shape
    parts = []
    for k in range(1, n):
        x, y, w, h, area = stats[k]
        if area < MIN_PART_AREA or x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue  # too small, or not provably enclosed (touches the crop edge)
        comp = lab == k
        ring = cv2.dilate(comp.astype(np.uint8), _K4).astype(bool) & ~comp
        if ring.any() and dark[ring].mean() >= MIN_RING_DARK:
            parts.append((k, stats[k, cv2.CC_STAT_TOP], stats[k, cv2.CC_STAT_TOP] + stats[k, cv2.CC_STAT_HEIGHT]))
    keep = np.zeros(fill.shape, np.uint8)
    if not parts:
        return keep
    # Digits share a baseline; drop enclosed bits of the icon above them.
    base = max(bottom for _, _, bottom in parts)
    for k, top, _ in parts:
        if top >= base - DIGIT_H:
            keep[lab == k] = 255
    return keep


def render_digits(mask: np.ndarray, gap: int = 6, pad: int = 8) -> np.ndarray | None:
    """Black-on-white image of the digits, re-spaced so thin glyphs don't merge."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    y0, y1 = ys.min(), ys.max() + 1
    # Glyphs = runs of columns containing ink (pieces of one glyph overlap in x).
    cols = mask[y0:y1].any(axis=0)
    runs, start = [], None
    for x, on in enumerate([*cols, False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            runs.append((start, x))
            start = None
    parts = []
    for a, b in runs:
        parts.append(mask[y0:y1, a:b])
        parts.append(np.zeros((y1 - y0, gap), np.uint8))
    strip = cv2.dilate(np.hstack(parts[:-1]), np.ones((2, 2), np.uint8))
    img = cv2.copyMakeBorder(255 - strip, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def parse_count(text: str) -> int | None:
    t = text.strip().replace("O", "0").replace("o", "0").replace("l", "1").replace("I", "1")
    t = re.sub(r"[\s.,:;'`\-]", "", t)  # re-spaced glyphs sometimes come back with separators
    if not re.fullmatch(r"\d{1,4}", t):
        return None
    return int(t)


def icon_signature(p: np.ndarray, slot: int) -> np.ndarray:
    """Coarse colour layout of the slot icon (4x2 cells x BGR), for change detection."""
    band = _cell(p, slot, ICON_BOX)
    return cv2.resize(band, (4, 2), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()


def icon_crop(p: np.ndarray, slot: int) -> np.ndarray:
    """The whole slot cell (reference px; the icon is drawn at 2x)."""
    return _cell(p, slot, (8, 8, 70, 72))


@dataclass
class SlotRead:
    count: int | None  # None = no count visible / unreadable
    signature: np.ndarray
    icon: np.ndarray


class QuickslotReader:
    """Reads all 8 slots; OCR runs only for slots whose digits changed."""

    def __init__(self, ocr):
        self.ocr = ocr  # RapidOCR instance
        self._cache: dict[bytes, int | None] = {}

    def _read_mask(self, mask: np.ndarray) -> int | None:
        if not mask.any():
            return None
        key = mask.tobytes()
        if key in self._cache:
            return self._cache[key]
        img = render_digits(mask)
        value = None
        if img is not None:
            r = self.ocr(img, use_det=False, use_cls=False, use_rec=True)
            if r.txts and r.scores[0] >= 0.7:
                value = parse_count(r.txts[0])
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[key] = value
        return value

    def read(self, frame: np.ndarray) -> list[SlotRead]:
        p = panel(frame)
        if p.shape[:2] != (PANEL_PX[1], PANEL_PX[0]):
            return []
        out = []
        for slot in range(COLS * ROWS):
            mask = digit_mask(_cell(p, slot, COUNT_BOX))
            out.append(SlotRead(self._read_mask(mask), icon_signature(p, slot), icon_crop(p, slot)))
        return out
