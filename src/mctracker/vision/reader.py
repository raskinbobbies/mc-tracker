"""Read game state from a frame of the MapleStory Classic window.

Regions are fractions of the client area, calibrated on 2560x1440 fullscreen
with UI Size = Ideal Ratio. Other resolutions/aspect ratios are unverified.
"""

from __future__ import annotations

import re
import time
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
    # Party HP widget: one row per member ("* name  [HP bar]"), stacking down,
    # room for a full party of 6. It's a movable window: this is only where it
    # starts (top right); ``find_party_widget`` finds it anywhere.
    "party": (0.840, 0.235, 0.998, 0.430),
    # Chat box, bottom left: party join/leave/create messages show only here.
    # Checked on 2000x1125 (16:9) screenshots of the default chat box position.
    "chat": (0.190, 0.775, 0.625, 0.905),
}
PARTY_SEARCH_EVERY_S = 9.0  # widget lost: search the whole frame at most this often
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
    chat: list[TextLine] = field(default_factory=list)
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


def _frame_boxes(img: np.ndarray, row_h: float, min_len: int, max_len: int | None = None,
                 x_from: int = 0) -> list[tuple[int, int, int, int]]:
    """HP bar frames, whatever the HP -> [(y0, y1, x0, x1)]: each bar sits in a
    white-bordered rectangle (inside a black outline) over a translucent grey
    track that the red fill covers from the left. Find rectangles whose top
    and bottom white edges span the same columns and whose left and right
    sides are white too.
    """
    c = img[:, :, :3].astype(np.int16)
    lo, hi = c.min(axis=2), c.max(axis=2)
    white = (lo >= 215) & (hi - lo <= 30)
    dark = hi <= 40
    edges = []  # (y, x0, x1) of long white runs
    for y in np.flatnonzero(white[:, x_from:].sum(axis=1) >= min_len):  # rows that can hold one
        for a, b in _runs(white[y, x_from:], min_len):
            if max_len is None or b - a <= max_len:
                edges.append((int(y), a + x_from, b + x_from))
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
                found.append((yt, yb + 1, a, b))
                break
    return found


def _bar_frames(img: np.ndarray) -> list[tuple[int, int]]:
    """HP bar frame rows in a party-widget crop."""
    h, w = img.shape[:2]
    boxes = _frame_boxes(img, h / 6.5, int(0.15 * w), x_from=int(0.4 * w))
    return _merge_rows([(y0, y1) for y0, y1, _, _ in boxes])


# The widget's layout at 2000x1125 (tests/test_party_widget.py): 316x219 px,
# HP bars 103 px wide at x=191, the first 27 px from the top, 27 px apart.
_WIDGET = dict(w=316, h=219, bar_x=191, bar_top=27, bar_w=103, pitch=27)


def find_party_widget(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Search the whole frame for the party widget (a movable window) by its
    column of HP-bar frames -> its box (x0, y0, x1, y1) in pixels, laid out
    like REGIONS['party'] so ``hp_bar_rows`` reads it. The column with the most
    bars wins; ties go to the one nearest the widget's default place."""
    h, w = frame.shape[:2]
    s = h / 1125
    L = {k: v * s for k, v in _WIDGET.items()}
    cols: list[list[tuple[int, int, int, int]]] = []
    for box in sorted(_frame_boxes(frame, L["h"] / 6.5, int(0.7 * L["bar_w"]), int(1.3 * L["bar_w"]) + 4)):
        y0, _, a, b = box
        col = next((c for c in cols if abs(c[-1][2] - a) <= 3 and abs(c[-1][3] - b) <= 3
                    and y0 - c[-1][0] <= 1.4 * L["pitch"]), None)
        if col is None:
            cols.append([box])
        elif y0 - col[-1][0] >= 0.6 * L["pitch"]:  # the next member, not the same frame again
            col.append(box)
    if not cols:
        return None
    dx, dy = REGIONS["party"][0] * w, REGIONS["party"][1] * h
    # Each member row has a leader star or member sun icon left of the name.
    b, g, r = (frame[:, :, i].astype(np.int16) for i in range(3))
    icon = (r > 200) & (g > 120) & (b < 100)
    x_icon = (round(-170 * s), round(-140 * s))  # icon columns, from the bar's left edge

    def icons(c) -> int:
        return sum(bool(icon[y0:y1, max(0, a + x_icon[0]):max(0, a + x_icon[1])].any()) for y0, y1, a, _ in c)

    best = max(cols, key=lambda c: (icons(c) > 0, len(c), -abs(c[0][2] - L["bar_x"] - dx) - abs(c[0][0] - L["bar_top"] - dy)))
    x0 = max(0, round(best[0][2] - L["bar_x"]))
    y0 = max(0, round(best[0][0] - L["bar_top"]))
    return x0, y0, min(w, x0 + round(L["w"])), min(h, y0 + round(L["h"]))


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
        self._party_box: tuple[int, int, int, int] | None = None  # where the widget was last found
        self._party_search_at = 0.0

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

    def read_chat(self, frame: np.ndarray, state: GameState) -> None:
        state.chat = self._lines(crop(frame, "chat"))

    def _party_crop(self, frame: np.ndarray) -> np.ndarray:
        if self._party_box is None:
            return crop(frame, "party")
        x0, y0, x1, y1 = self._party_box
        return frame[y0:y1, x0:x1]

    def read_party(self, frame: np.ndarray, state: GameState) -> None:
        img = self._party_crop(frame)
        bars = hp_bar_rows(img)
        if not bars and time.monotonic() >= self._party_search_at:
            # Lost (moved, or no party): look for it anywhere, now and then.
            self._party_search_at = time.monotonic() + PARTY_SEARCH_EVERY_S
            if (box := find_party_widget(frame)) is not None:
                self._party_box = box
                img = self._party_crop(frame)
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
