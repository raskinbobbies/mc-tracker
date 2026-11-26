"""Mesos picked up, from the "You have gained mesos (+N)" lines.

Each mesos line is followed on screen as a *track* while it scrolls up, and
booked once per track, so a line read again (hidden for a few frames, or the
scroll lost) or misread is never a second pickup. Rules (see decisions.md,
"2026-11-26 — Mesos: one booking per line on screen"):

1. Strict parse: the amount must be closed by ")" ("(+7o))" is no amount).
2. The area's scroll since the last frame is the shift that lines up the
   most messages of the same kind and value, less those it puts on another
   message (messages never change in place), each weighted by how rare it is
   in the frame. Tracks not read this frame move up with it; when nothing
   lines up (the area was replaced), tracks not read last frame are gone.
3. A row matches a track that has read its value, at or above where the
   track is (messages only scroll up), nearest first, one row per track.
4. A row no track takes is a new line, booked once its value has been read
   ``CONFIRM`` times (a one-off misread never is). Two identical pickups in a
   row are two rows on screen together (the older one scrolled up as the
   newer came in), so one is always left over and booked; if the older one
   isn't read in that frame, the newer is booked once both are read.
5. A track's value is the value read most often (ties: the earliest); a
   change of it corrects the booking.
6. A track is gone once unseen for ``GONE_S``, or when the area has been
   empty for ``EMPTY_FRAMES`` frames (faded) and it was first read over
   ``YOUNG_S`` ago (a message stays up longer, so a younger one was only
   unread).
7. A value over ``SPIKE`` times the median of the recent pickups, whose last
   digit dropped lands within 2x of that median, is a ")" read as a digit:
   the digit is dropped.
"""

from __future__ import annotations

import math
import re
from collections import Counter, deque
from statistics import median

from .kills import MESOS_RE, _int, parse_message, visible_rows

GONE_S = 3.0
YOUNG_S = 6.0
EMPTY_FRAMES = 3
SPIKE = 8
RECENT = 20  # pickups the median is taken over
MIN_RECENT = 5
CONFIRM = 2  # reads of the same value before a line is booked
_CLOSE = re.compile(r"\s*\)")


def parse_mesos(text: str) -> int | None:
    """The amount of a mesos line, only when the number is closed by ")"."""
    m = MESOS_RE.search(text)
    if not m or not _CLOSE.match(text, m.end(1)):
        return None
    return _int(m.group(1)) or None


def _key(text: str) -> tuple | None:
    """What a row says, for lining rows up (None: not a message). Item names are left out: OCR
    garbles them from one read to the next."""
    m = parse_message(text)
    return None if m is None else (m.kind, m.value)


def _shift(prev: list[tuple[tuple, float]], cur: list[tuple[tuple, float]], tol: float) -> tuple[float, int]:
    """How far the area scrolled up since ``prev``: the shift that lines up the most rows of the
    same text, less the rows it puts on another text (messages never change in place), each
    weighted by how rare its text is (a repeating kill-and-pickup pattern lines up at several
    shifts). Ties: the smallest. -> (shift, rows lined up)."""
    n = Counter(k for k, _ in cur)
    best = (-math.inf, 0.0, 0.0, 0)  # (score, -|shift|, shift, rows lined up)
    for d in {round(py - y, 1) for k, y in cur for pk, py in prev if pk == k and py - y >= -tol}:
        used: set[int] = set()
        score, lined = 0.0, 0
        for k, y in cur:
            j = next((j for j, (pk, py) in enumerate(prev)
                      if j not in used and pk == k and abs(py - d - y) <= tol), None)
            if j is not None:
                used.add(j)
                lined += 1
                score += 1 / n[k]
            elif any(abs(py - d - y) <= tol for _, py in prev):
                score -= 1 / n[k]  # another text where this row was: messages never change in place
        best = max(best, (score, -abs(d), d, lined))
    return best[2], best[3]


class _Track:
    __slots__ = ("votes", "order", "y", "first", "last", "frame", "booked", "for_value")

    def __init__(self, v: int, y: float, t: float, f: int) -> None:
        self.votes: Counter[int] = Counter({v: 1})
        self.order = [v]  # values in the order first read (ties go to the earliest)
        self.y, self.first, self.last, self.frame = y, t, t, f
        self.booked = 0  # mesos booked for it
        self.for_value: int | None = None  # the value read that booking is for

    @property
    def value(self) -> int:
        best = max(self.votes.values())
        return next(v for v in self.order if self.votes[v] == best)

    def read(self, v: int, y: float, t: float, f: int) -> None:
        if v not in self.votes:
            self.order.append(v)
        self.votes[v] += 1
        self.y, self.last, self.frame = y, t, f


class MesosLines:
    """Mesos booked per line on screen; ``update`` returns the change to the total."""

    def __init__(self) -> None:
        self._tracks: list[_Track] = []
        self._empty = 0
        self._tol = None  # half a row, from the last frame that showed one
        self._recent: deque[int] = deque(maxlen=RECENT)
        self._prev: list[tuple[tuple, float]] = []  # (what it says, y) of the last frame with messages
        self._f = 0  # frames read

    def _plausible(self, v: int) -> int:
        if len(self._recent) >= MIN_RECENT:
            med = median(self._recent)
            if v > SPIKE * med and med / 2 <= v // 10 <= 2 * med:
                return v // 10
        return v

    def update(self, lines, now: float) -> int:
        rows = visible_rows(lines)
        hs = sorted(h for _, _, h in rows if h)
        gaps = [b[1] - a[1] for a, b in zip(rows, rows[1:]) if b[1] > a[1]]
        if hs or gaps:
            self._tol = 0.45 * (hs[len(hs) // 2] if hs else min(gaps))
        tol = self._tol if self._tol is not None else 0.45
        self._empty = 0 if rows else self._empty + 1
        faded = self._empty >= EMPTY_FRAMES
        self._tracks = [x for x in self._tracks if now - x.last <= GONE_S
                        and not (faded and now - x.first >= YOUNG_S)]
        self._f += 1
        f = self._f
        cur = [(k, y) for text, y, _ in rows if (k := _key(text))]
        if cur:
            d, lined = _shift(self._prev, cur, tol)
            for x in self._tracks:
                x.y -= d  # the area scrolled: lines not read this frame moved up too
                if self._prev and not lined and x.frame != f - 1:
                    x.y = -math.inf  # nothing lines up: the area was replaced, so it is gone
            self._prev = cur
        found = [(v, y) for text, y, _ in rows if (v := parse_mesos(text))]
        # Rows to tracks that read their value, at or above them, nearest first (rule 3).
        pairs = sorted((abs(x.y - y), i, j)
                       for i, (v, y) in enumerate(found) for j, x in enumerate(self._tracks)
                       if y <= x.y + tol and v in x.votes)
        took: dict[int, int] = {}
        used: set[int] = set()
        for _, i, j in pairs:
            if i not in took and j not in used:
                took[i] = j
                used.add(j)
        delta = 0
        for i, (v, y) in enumerate(found):
            if i in took:
                self._tracks[took[i]].read(v, y, now, f)
            else:
                self._tracks.append(_Track(v, y, now, f))
        for x in self._tracks:
            if x.value == x.for_value or x.votes[x.value] < CONFIRM:
                continue  # a value read once may be a misread: wait for a second read
            v = self._plausible(x.value)
            if x.for_value is None:
                self._recent.append(v)
            delta += v - x.booked
            x.booked, x.for_value = v, x.value
        return delta
