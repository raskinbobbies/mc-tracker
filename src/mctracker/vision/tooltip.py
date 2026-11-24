"""Find the hovered item's name in an item tooltip.

Item tooltips are big dark-navy, semi-opaque panels. We locate those panels
first and OCR only each panel's title line (the large text at its top left), so
name tags, chat, the inventory window and damage numbers are never read. When
the game also shows a comparison tooltip for the equipped item, the hovered
item's tooltip is the left one, so boxes are returned left to right.

If no panel is found (other UI scale/theme) we fall back to OCRing the area
around the cursor and keeping the largest text that resolves to an item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Callable

import cv2
import numpy as np

from .reader import Reader, TextLine

# Fallback search box around the cursor, as fractions of frame width/height.
AROUND_CURSOR = (0.45, 0.45)  # half-width, half-height


@dataclass(frozen=True)
class BoxThresholds:
    """Tunables for finding item-tooltip panels. Sizes are fractions of the frame.

    Calibrated on 2560x1440 (checked at 2000x1125 too): the panel background is
    blue-dominant (B ~ 90-125, G/R ~ 35-80 whatever the scenery behind it).
    """

    min_blue_lead: int = 10  # B must exceed both G and R by at least this
    min_blue: int = 55  # ...and B itself is at least this bright
    max_blue: int = 175  # brighter blue is sky/UI art, not the dim panel
    min_width: float = 0.10  # of frame width
    max_width: float = 0.35
    min_height: float = 0.09  # of frame height
    max_height: float = 0.60
    min_fill: float = 0.65  # share of a panel's pixels that are panel-coloured (text and icons are holes)
    split_density: float = 0.30  # a column/row below this is a gap between panels, not part of one
    row_merge_gap: float = 0.02  # row bands closer than this (of frame height) are one panel: a weak divider under the class row isn't a split
    title_height: float = 0.040  # title strip below the panel top, of frame height
    title_inset: float = 0.004  # skip the panel border (of frame height)


BOX_THRESHOLDS = BoxThresholds()


def _clean(text: str) -> str:
    # Drop the leading bullet and stray symbols OCR picks up.
    return re.sub(r"^[^A-Za-z0-9(]+", "", text).strip()


_ENHANCE_RE = re.compile(r"\s*[(\[]\s*\+\s*\d+\s*[)\]]?\s*$")


def strip_enhancement(text: str) -> str:
    """'Bazlud (+7)' -> 'Bazlud'."""
    return _ENHANCE_RE.sub("", text).strip()


def _looks_like_name(text: str) -> bool:
    s = _clean(text)
    return 3 <= len(s) <= 40 and ":" not in s and not s.startswith("+") and sum(c.isalpha() for c in s) >= 3


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) spans where flags is True."""
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def _merge_runs(runs: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    """Join spans whose gap is at most max_gap."""
    out: list[tuple[int, int]] = []
    for a, b in runs:
        if out and a - out[-1][1] <= max_gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def find_tooltip_boxes(frame: np.ndarray, th: BoxThresholds = BOX_THRESHOLDS) -> list[tuple[int, int, int, int]]:
    """Item-tooltip panels as (x0, y0, x1, y1), sorted left to right."""
    h, w = frame.shape[:2]
    b, g, r = (frame[:, :, i].astype(np.int16) for i in range(3))
    lead = b - np.maximum(g, r)
    mask = ((lead >= th.min_blue_lead) & (b >= th.min_blue) & (b <= th.max_blue)).astype(np.uint8) * 255
    # Drop thin lines (window frames), then bridge text strokes and icons.
    k = max(3, round(0.0015 * w)) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < th.min_width * w or bh < th.min_height * h:
            continue
        on = mask[y: y + bh, x: x + bw] > 0
        # Two tooltips side by side touch: split where a column is mostly background.
        for c0, c1 in _runs(on.mean(axis=0) >= th.split_density):
            cols = on[:, c0:c1]
            # Text rows are sparse; the divider under the class row can be too, so bridge short gaps.
            rows = _merge_runs(_runs(cols.mean(axis=1) >= th.split_density / 2), round(th.row_merge_gap * h))
            if not rows:
                continue
            r0, r1 = max(rows, key=lambda t: t[1] - t[0])  # tallest band (top = the header, not a divider)
            piece = cols[r0:r1]
            pw, ph = c1 - c0, r1 - r0
            if not (th.min_width * w <= pw <= th.max_width * w and th.min_height * h <= ph <= th.max_height * h):
                continue
            if piece.mean() < th.min_fill:  # text/icons are holes; a real panel is mostly filled
                continue
            boxes.append((int(x + c0), int(y + r0), int(x + c1), int(y + r1)))
    return sorted(boxes)


def _title_strip(frame: np.ndarray, box: tuple[int, int, int, int], th: BoxThresholds) -> np.ndarray:
    h = frame.shape[0]
    x0, y0, x1, y1 = box
    top = y0 + round(th.title_inset * h)
    bottom = min(y1, y0 + round(th.title_height * h))
    return frame[top:bottom, x0:x1]


def _title_of(reader: Reader, strip: np.ndarray) -> str | None:
    lines = [l for l in reader._lines(strip, upscale=1.0) if _looks_like_name(l.text)]
    if not lines:
        return None
    return _clean(max(lines, key=lambda l: l.height).text)


@dataclass(frozen=True)
class _PanelRead:
    box: tuple[int, int, int, int]
    name: str  # "" when the title couldn't be read
    item_id: int | None


def _read_panels(
    reader: Reader,
    frame: np.ndarray,
    boxes: list[tuple[int, int, int, int]],
    resolve: Callable[[str], int | None] | None,
    th: BoxThresholds,
) -> list[_PanelRead]:
    out: list[_PanelRead] = []
    for box in boxes:  # already left to right
        title = _title_of(reader, _title_strip(frame, box, th))
        if not title:
            out.append(_PanelRead(box, "", None))  # keep the slot so panel order survives
            continue
        name = strip_enhancement(title)
        item_id = resolve(name) if resolve else None
        if item_id is None and name != title:
            item_id = resolve(title) if resolve else None
        out.append(_PanelRead(box, name, item_id))
    return out


def _cursor_distance(box: tuple[int, int, int, int], cursor: tuple[int, int] | None) -> float:
    """0 when the cursor is inside the box, else the gap to its nearest edge."""
    if cursor is None:
        return 0.0
    x0, y0, x1, y1 = box
    dx = max(x0 - cursor[0], 0, cursor[0] - x1)
    dy = max(y0 - cursor[1], 0, cursor[1] - y1)
    return float((dx * dx + dy * dy) ** 0.5)


def _names_from_boxes(
    reader: Reader,
    frame: np.ndarray,
    boxes: list[tuple[int, int, int, int]],
    resolve: Callable[[str], int | None] | None,
    th: BoxThresholds,
    cursor: tuple[int, int] | None = None,
) -> list[tuple[str, int | None]]:
    """Candidates from the panels, hovered first; [] when no panel title resolves to an item.

    Panels whose title resolves come first (a false panel's label never does).
    The hovered one is the resolved panel containing or nearest the cursor, then
    left to right. A panel whose title can't be read stays in the ranking as a
    ("", None) placeholder, so an unreadable hovered panel isn't replaced by
    the comparison panel.
    """
    panels = _read_panels(reader, frame, boxes, resolve, th)
    if resolve is None:
        real = [p for p in panels if p.name]  # nothing to check against: any readable title counts
    else:
        real = [p for p in panels if p.item_id is not None]
    if not real:
        return []
    ranked = [p for p in panels if p in real or not p.name]  # resolved panels + unreadable placeholders
    ranked.sort(key=lambda p: (_cursor_distance(p.box, cursor), p.box[0]))
    first, rest = ranked[0], sorted(ranked[1:], key=lambda p: p.box[0])
    leftovers = [p for p in panels if p.name and p not in real]  # readable but unresolved
    return [(p.name, p.item_id) for p in [first, *rest, *leftovers]]


def _names_around_cursor(
    reader: Reader,
    frame: np.ndarray,
    cursor: tuple[int, int] | None,
    resolve: Callable[[str], int | None] | None,
    max_lookups: int,
) -> list[tuple[str, int | None]]:
    h, w = frame.shape[:2]
    region = frame
    if cursor is not None:
        cx, cy = cursor
        hw, hh = int(AROUND_CURSOR[0] * w), int(AROUND_CURSOR[1] * h)
        region = frame[max(0, cy - hh): min(h, cy + hh), max(0, cx - hw): min(w, cx + hw)]
    lines = [l for l in reader._lines(region, upscale=1.0) if _looks_like_name(l.text)]
    lines.sort(key=lambda l: -l.height)
    found: list[tuple[TextLine, str, int | None]] = []
    for l in lines[:max_lookups]:
        name = strip_enhancement(_clean(l.text))
        found.append((l, name, resolve(name) if resolve else None))
    resolved = [f for f in found if f[2] is not None] or found
    if not resolved:
        return []
    # Keep the title-sized ones, then order top-to-bottom, left-to-right.
    tallest = max(f[0].height for f in resolved)
    titles = [f for f in resolved if f[0].height >= 0.85 * tallest]
    row = median(f[0].height for f in titles) or 1
    # Cluster titles into rows (same y within half a line), then left-to-right.
    titles.sort(key=lambda f: f[0].y)
    rows: list[list] = []
    for f in titles:
        if rows and abs(f[0].y - rows[-1][0][0].y) < row / 2:
            rows[-1].append(f)
        else:
            rows.append([f])
    titles = [f for r in rows for f in sorted(r, key=lambda f: f[0].x0)]
    rest = [f for f in resolved if f not in titles]
    return [(n, i) for _, n, i in titles + rest]


def hovered_item_names(
    reader: Reader,
    frame: np.ndarray,
    cursor: tuple[int, int] | None = None,
    resolve: Callable[[str], int | None] | None = None,
    max_lookups: int = 16,
    thresholds: BoxThresholds = BOX_THRESHOLDS,
) -> list[tuple[str, int | None]]:
    """Return (name, item_id) candidates, best (hovered) first.

    Reads the title of each item-tooltip panel. Panels whose title resolves to
    an item win; among them the one containing or nearest the cursor is first
    (the hovered item's tooltip sits just right of or below the cursor, left of
    the equipped-item comparison), then left to right. An unreadable title is a
    ("", None) placeholder. With no resolvable panel, OCR the cursor area.
    """
    boxes = find_tooltip_boxes(frame, thresholds)
    if boxes:
        names = _names_from_boxes(reader, frame, boxes, resolve, thresholds, cursor)
        if names:
            return names
    return _names_around_cursor(reader, frame, cursor, resolve, max_lookups)
