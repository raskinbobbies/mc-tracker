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
}
UPSCALE = 2.0


@dataclass
class TextLine:
    text: str
    y: float  # center, in region pixels (pre-upscale)
    score: float


@dataclass
class GameState:
    level: int | None = None
    exp: int | None = None
    exp_percent: float | None = None
    street: str | None = None
    map_name: str | None = None
    messages: list[TextLine] = field(default_factory=list)


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

    def _lines(self, img: np.ndarray) -> list[TextLine]:
        """Detect + recognize a variable number of lines (message area)."""
        if img.size == 0:
            return []
        r = self.ocr(self._prep(img), use_det=True, use_cls=False, use_rec=True)
        if not r.txts:
            return []
        out = []
        for box, txt, score in zip(r.boxes, r.txts, r.scores):
            ys = [p[1] for p in box]
            out.append(TextLine(txt, (min(ys) + max(ys)) / 2 / UPSCALE, float(score)))
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

    def read_all(self, frame: np.ndarray) -> GameState:
        s = GameState()
        self.read_status(frame, s)
        self.read_map(frame, s)
        self.read_messages(frame, s)
        return s
