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
    # Only its newest 3 lines (of 5), left 60% (a party line is short), each
    # read at its fixed row without text detection: 150-230 ms a read instead
    # of ~1 s for the whole box with detection. Checked on 2000x1125 (16:9)
    # screenshots of the default chat box position: lines 25 px apart.
    "chat": (0.190, 0.839, 0.451, 0.906),
}
CHAT_LINES = 3  # rows in REGIONS["chat"]
PARTY_SEARCH_EVERY_S = 9.0  # widget lost: search the whole frame at most this often
PARTY_SEARCH_SLOW_S = 30.0  # ... and this often once it has missed a few times (playing solo)
PARTY_SEARCH_MISSES = 3
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
    party: int = 0  # party members (HP-bar rows); 0 = no party widget


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


def _masks(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(white, dark) pixel masks: HP bar frames are white inside a black outline."""
    b, g, r = cv2.split(np.ascontiguousarray(img[:, :, :3]))
    lo, hi = cv2.min(cv2.min(b, g), r), cv2.max(cv2.max(b, g), r)
    return (lo >= 215) & (cv2.subtract(hi, lo) <= 30), hi <= 40


def _frame_boxes(img: np.ndarray, row_h: float, min_len: int, max_len: int | None = None,
                 x_from: int = 0, masks: tuple[np.ndarray, np.ndarray] | None = None,
                 outline_req: float = 0.7) -> list[tuple[int, int, int, int]]:
    """HP bar frames, whatever the HP -> [(y0, y1, x0, x1)]: each bar sits in a
    white-bordered rectangle (inside a black outline) over a translucent grey
    track that the red fill covers from the left. Find rectangles whose top
    and bottom white edges span the same columns and whose left and right
    sides are white too.
    """
    white, dark = _masks(img) if masks is None else masks
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
            if left >= 0.7 and right >= 0.7 and body < 0.5 and outline >= outline_req:
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


def _pool2(mask: np.ndarray) -> np.ndarray:
    """Half-resolution mask, a pixel set if any of its 2x2 block is: thin
    1-2 px frame edges survive (plain subsampling can drop them)."""
    h, w = (mask.shape[0] // 2) * 2, (mask.shape[1] // 2) * 2
    return mask[:h, :w].reshape(h // 2, 2, w // 2, 2).any(axis=(1, 3))


def _bar_boxes(frame: np.ndarray, L: dict) -> list[tuple[int, int, int, int]]:
    """HP-bar-sized frames anywhere in the frame. A coarse pass on half-size
    masks finds candidates (most of the cost of a full-res pass is scanning
    speckled play field); each is then confirmed at full resolution."""
    row_h, lo, hi = L["h"] / 6.5, int(0.7 * L["bar_w"]), int(1.3 * L["bar_w"]) + 4
    white, dark = _masks(frame)
    coarse = _frame_boxes(None, row_h / 2, lo // 2, hi // 2 + 2, masks=(_pool2(white), _pool2(dark)),
                          outline_req=0.0)  # pooling smears the 1 px outline into the edge: check it below
    h, w = white.shape
    out = set()
    for y0, y1, a, b in coarse:
        ys, xs = slice(max(0, 2 * y0 - 6), min(h, 2 * y1 + 6)), slice(max(0, 2 * a - 6), min(w, 2 * b + 6))
        for fy0, fy1, fa, fb in _frame_boxes(None, row_h, lo, hi, masks=(white[ys, xs], dark[ys, xs])):
            out.add((fy0 + ys.start, fy1 + ys.start, fa + xs.start, fb + xs.start))
    return sorted(out)


def find_party_widget(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Search the whole frame for the party widget (a movable window) by its
    column of HP-bar frames -> its box (x0, y0, x1, y1) in pixels, laid out
    like REGIONS['party'] so ``hp_bar_rows`` reads it. The column with the most
    bars wins; ties go to the one nearest the widget's default place."""
    h, w = frame.shape[:2]
    s = h / 1125
    L = {k: v * s for k, v in _WIDGET.items()}
    cols: list[list[tuple[int, int, int, int]]] = []
    for box in _bar_boxes(frame, L):
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
    x_icon = (round(-170 * s), round(-140 * s))  # icon columns, from the bar's left edge
    best = max(cols, key=lambda c: (_icons(frame, c, x_icon) > 0, len(c),
                                    -abs(c[0][2] - L["bar_x"] - dx) - abs(c[0][0] - L["bar_top"] - dy)))
    x0 = max(0, round(best[0][2] - L["bar_x"]))
    y0 = max(0, round(best[0][0] - L["bar_top"]))
    return x0, y0, min(w, x0 + round(L["w"])), min(h, y0 + round(L["h"]))


def _icons(frame: np.ndarray, col, x_icon: tuple[int, int]) -> int:
    """How many of a column's rows have the leader star / member sun icon
    (orange) left of the name."""
    n = 0
    for y0, y1, a, _ in col:
        p = frame[y0:y1, max(0, a + x_icon[0]):max(0, a + x_icon[1]), :3].astype(np.int16)
        n += bool(((p[..., 2] > 200) & (p[..., 1] > 120) & (p[..., 0] < 100)).any())
    return n


def _merge_rows(rows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for y0, y1 in sorted(rows):
        if out and y0 <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], y1))
        else:
            out.append((y0, y1))
    return out


def hp_bar_rows(img: np.ndarray) -> list[tuple[int, int]]:
    """Find party members' HP bars in the party region -> their (y0, y1) rows,
    top to bottom. A row is found by its bar's frame (white border in a black
    outline), so members at low HP or dead (little or no red) still count.
    Red alone never counts: damage numbers and skill effects over open play
    field are red too (a solo player read as a party)."""
    if img.size == 0:
        return []
    return _bar_frames(img)


class Reader:
    def __init__(self):
        from rapidocr import RapidOCR  # heavy import; load once

        self.ocr = RapidOCR()
        self._party_box: tuple[int, int, int, int] | None = None  # where the widget was last found
        self._party_search_at = 0.0
        self._party_misses = 0  # full-frame searches in a row that found nothing
        self._party_rows = 0  # rows in the last read that had any
        self._chat_sig: bytes | None = None

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

    def read_chat(self, frame: np.ndarray, state: GameState) -> bool:
        """OCR the chat strip -> False (``state.chat`` untouched) if its text
        pixels haven't changed since the last read: nothing new to read."""
        img = crop(frame, "chat")
        sig = (img[::2, ::2, :3].max(axis=2) >= 200).tobytes()  # text is bright; the panel is dim
        if sig == self._chat_sig:
            return False
        self._chat_sig = sig
        pitch = img.shape[0] / CHAT_LINES
        state.chat = [TextLine(t, (i + 0.5) * pitch, 1.0, 0.0, float(img.shape[1]), pitch)
                      for i in range(CHAT_LINES)
                      if (t := self._line(img[round(i * pitch):round((i + 1) * pitch)]).strip())]
        return True

    def _party_crop(self, frame: np.ndarray) -> np.ndarray:
        if self._party_box is None:
            return crop(frame, "party")
        x0, y0, x1, y1 = self._party_box
        return frame[y0:y1, x0:x1]

    def read_party(self, frame: np.ndarray, state: GameState) -> None:
        now = time.monotonic()
        img = self._party_crop(frame)
        bars = hp_bar_rows(img)
        # Search the whole frame when the widget isn't where it was, or has
        # lost rows there (moved partly out of the cached box), now and then.
        lost_rows = self._party_box is not None and 0 < len(bars) < self._party_rows
        if (not bars or lost_rows) and now >= self._party_search_at:
            box = find_party_widget(frame)
            if box is None:
                self._party_misses += 1
                self._party_box = None  # don't keep reading a stale spot of play field
            else:
                self._party_misses = 0
                x0, y0, x1, y1 = box
                if len(found := hp_bar_rows(frame[y0:y1, x0:x1])) >= len(bars):
                    self._party_box, img, bars = box, frame[y0:y1, x0:x1], found
            slow = self._party_misses >= PARTY_SEARCH_MISSES  # solo: back off
            self._party_search_at = now + (PARTY_SEARCH_SLOW_S if slow else PARTY_SEARCH_EVERY_S)
        self._party_rows = len(bars)
        # Rows only: who is in the party isn't used, and OCR of the names cost
        # ~180 ms a read (786 ms the first time).
        state.party = len(bars)

    def read_all(self, frame: np.ndarray) -> GameState:
        s = GameState()
        self.read_status(frame, s)
        self.read_map(frame, s)
        self.read_messages(frame, s)
        self.read_party(frame, s)
        return s
