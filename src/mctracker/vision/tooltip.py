"""Find the hovered item's name in an item tooltip.

Tooltips are translucent (their colour depends on the background), so instead
of locating the panel we OCR the area around the cursor, keep lines set in the
larger title font, and keep those that resolve to a real item. When the game
shows a comparison tooltip for the equipped item, the hovered item's tooltip
is the left one.
"""

from __future__ import annotations

import re
from statistics import median
from typing import Callable

import numpy as np

from .reader import Reader, TextLine

# Search box around the cursor, as fractions of frame width/height.
AROUND_CURSOR = (0.45, 0.45)  # half-width, half-height


def _clean(text: str) -> str:
    # Drop the leading bullet and stray symbols OCR picks up.
    return re.sub(r"^[^A-Za-z0-9(]+", "", text).strip()


def _looks_like_name(text: str) -> bool:
    s = _clean(text)
    return 3 <= len(s) <= 40 and ":" not in s and not s.startswith("+") and sum(c.isalpha() for c in s) >= 3


def hovered_item_names(
    reader: Reader,
    frame: np.ndarray,
    cursor: tuple[int, int] | None = None,
    resolve: Callable[[str], int | None] | None = None,
    max_lookups: int = 16,
) -> list[tuple[str, int | None]]:
    """Return (name, item_id) candidates, best (hovered) first.

    Item titles use the largest font in a tooltip, and the hovered item's
    tooltip sits left of the equipped-item comparison tooltip.
    """
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
        name = _clean(l.text)
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
