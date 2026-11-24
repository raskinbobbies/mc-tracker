"""Read game state from a frame of the MapleStory Classic window.

Regions are fractions of the client area, calibrated on 2560x1440 fullscreen
with UI Size = Ideal Ratio. Other resolutions/aspect ratios are unverified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

# (x0, y0, x1, y1) as fractions of width/height
REGIONS = {
    "level": (0.190, 0.956, 0.250, 0.996),
    "status_exp": (0.530, 0.951, 0.630, 0.978),
    "minimap_street": (0.040, 0.043, 0.170, 0.066),
    "minimap_name": (0.040, 0.066, 0.170, 0.089),
    "messages": (0.780, 0.530, 1.000, 0.800),
    # Party HP widget, top right: one row per member ("* name  [HP bar]"),
    # stacking down. Room for a full party of 6.
    "party": (0.840, 0.235, 0.998, 0.430),
}
UPSCALE = 2.0


@dataclass
class TextLine:
    text: str
    y: float  # center, in region pixels (pre-upscale)
    score: float
    x0: float = 0.0
    x1: float = 0.0
    height: float = 0.0


@dataclass
class GameState:
    level: int | None = None
    exp: int | None = None
    exp_percent: float | None = None
    street: str | None = None
    map_name: str | None = None
    messages: list[TextLine] = field(default_factory=list)
    party: list[str] = field(default_factory=list)  # member names; empty = no party widget


_STATUS_RE = re.compile(r"([\d][\d,.\s]*)\s*\[\s*([\d.,]+)\s*%")


def parse_status_exp(text: str) -> tuple[int, float] | None:
    """'EXP.66,700[14.24%]' -> (66700, 14.24). OCR may read ',' as '.'."""
    t = text.replace("EXP", "", 1)
    m = _STATUS_RE.search(t)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    pct = m.group(2).replace(",", ".")
    try:
        return int(digits), float(pct)
    except ValueError:
        return None


def parse_level(texts: list[str]) -> int | None:
    for t in texts:
        d = re.sub(r"\D", "", t)
        if d and 1 <= int(d) <= 300:
            return int(d)
    return None


def crop(frame: np.ndarray, region: str) -> np.ndarray:
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = REGIONS[region]
    return frame[int(y0 * h): int(y1 * h), int(x0 * w): int(x1 * w)]


def _runs(row: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """[(x0, x1)] of True runs at least ``min_len`` long (x1 exclusive)."""
    padded = np.concatenate(([0], row.astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(padded))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= min_len]


def _bar_frames(img: np.ndarray) -> list[tuple[int, int]]:
    """HP bar frames, whatever the HP: each bar sits in a white-bordered
    rectangle (inside a black outline) over a translucent grey track that the
    red fill covers from the left. Find rectangles whose top and bottom white
    edges span the same columns and whose left and right sides are white too.
    """
    h, w = img.shape[:2]
    row_h = h / 6.5
    c = img[:, :, :3].astype(int)
    white = (c.min(axis=2) >= 215) & (c.max(axis=2) - c.min(axis=2) <= 30)
    dark = c.max(axis=2) <= 40
    x_from = int(0.4 * w)
    edges = []  # (y, x0, x1) of long white runs
    for y in range(h):
        for a, b in _runs(white[y, x_from:], int(0.15 * w)):
            edges.append((y, a + x_from, b + x_from))
    found = []
    for i, (yt, a, b) in enumerate(edges):
        for yb, a2, b2 in edges[i + 1:]:
            if yb - yt > 0.7 * row_h:
                break
            if yb - yt < 0.2 * row_h or abs(a - a2) > 3 or abs(b - b2) > 3:
                continue
            inner = slice(yt + 1, yb)
            left = white[inner, max(0, a - 3):a + 3].any(axis=1).mean()
            right = white[inner, b - 3:b + 3].any(axis=1).mean()
            # Not a white box (e.g. a window title bar): the inside is fill or
            # track, and a black outline runs just above the white edge.
            body = white[yt + 2:yb - 1, a + 3:b - 3].mean() if yb - yt > 4 else 1.0
            outline = max((dark[y, a:b].mean() for y in range(max(0, yt - 2), yt)), default=0.0)
            if left >= 0.7 and right >= 0.7 and body < 0.5 and outline >= 0.7:
                found.append((yt, yb + 1))
                break
    return _merge_rows(found)


def _merge_rows(rows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for y0, y1 in sorted(rows):
        if out and y0 <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], y1))
        else:
            out.append((y0, y1))
    return out


def _red_fills(img: np.ndarray) -> list[tuple[int, int]]:
    """Red HP fills: thin, wide, saturated red rectangles (needs enough HP)."""
    h, w = img.shape[:2]
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    mask = ((r > 150) & (g < 70) & (b < 70)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    row_h = h / 6.5
    rows = []
    for x, y, bw, bh, area in stats[1:n]:
        if (0.15 * row_h <= bh <= 0.7 * row_h and bw >= 2.5 * bh and x > 0.4 * w
                and area >= 0.6 * bw * bh):
            rows.append((int(y), int(y + bh)))
    return rows


def hp_bar_rows(img: np.ndarray) -> list[tuple[int, int]]:
    """Find party members' HP bars in the party region -> their (y0, y1) rows,
    top to bottom. A row is found by its bar's frame, so members at low HP or
    dead (little or no red) still count; a red fill also counts in case the
    frame is hard to see."""
    if img.size == 0:
        return []
    return _merge_rows(_bar_frames(img) + _red_fills(img))


class Reader:
    def __init__(self):
        from rapidocr import RapidOCR  # heavy import; load once

        self.ocr = RapidOCR()

    @staticmethod
    def _prep(img: np.ndarray) -> np.ndarray:
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return cv2.resize(img, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)

    def _line(self, img: np.ndarray) -> str:
        """Recognize a single fixed-position text line (no detection: ~20 ms)."""
        if img.size == 0:
            return ""
        r = self.ocr(self._prep(img), use_det=False, use_cls=False, use_rec=True)
        return r.txts[0] if r.txts else ""

    def _lines(self, img: np.ndarray, upscale: float = UPSCALE) -> list[TextLine]:
        """Detect + recognize a variable number of lines."""
        if img.size == 0:
            return []
        if img.ndim == 3 and img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        if upscale != 1:
            img = cv2.resize(img, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
        r = self.ocr(img, use_det=True, use_cls=False, use_rec=True)
        if not r.txts:
            return []
        out = []
        for box, txt, score in zip(r.boxes, r.txts, r.scores):
            xs = [p[0] / upscale for p in box]
            ys = [p[1] / upscale for p in box]
            out.append(TextLine(txt, (min(ys) + max(ys)) / 2, float(score),
                                min(xs), max(xs), max(ys) - min(ys)))
        out.sort(key=lambda l: l.y)  # top (oldest) -> bottom (newest)
        return out

    def read_status(self, frame: np.ndarray, state: GameState) -> None:
        state.level = parse_level([self._line(crop(frame, "level"))])
        if p := parse_status_exp(self._line(crop(frame, "status_exp"))):
            state.exp, state.exp_percent = p

    def read_map(self, frame: np.ndarray, state: GameState) -> None:
        state.street = self._line(crop(frame, "minimap_street")) or None
        state.map_name = self._line(crop(frame, "minimap_name")) or None

    def read_messages(self, frame: np.ndarray, state: GameState) -> None:
        state.messages = self._lines(crop(frame, "messages"))

    def read_party(self, frame: np.ndarray, state: GameState) -> None:
        img = crop(frame, "party")
        bars = hp_bar_rows(img)
        if not bars:
            state.party = []
            return
        names = []
        lines = self._lines(img[:, : int(img.shape[1] * 0.62)])
        for y0, y1 in bars:
            row = [l.text.strip("*☆★ ") for l in lines if y0 - (y1 - y0) <= l.y <= y1 + (y1 - y0)]
            names.append(" ".join(t for t in row if t) or "?")
        state.party = names

    def read_all(self, frame: np.ndarray) -> GameState:
        s = GameState()
        self.read_status(frame, s)
        self.read_map(frame, s)
        self.read_messages(frame, s)
        self.read_party(frame, s)
        return s
