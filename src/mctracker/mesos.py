"""Mesos picked up, from the "You have gained mesos (+N)" lines.

Each mesos line is followed on screen as a *track* while it scrolls up, and
booked once per track, so a line read again (hidden for a few frames, or the
scroll lost) or misread is never a second pickup. Rules (see decisions.md,
"2026-11-26 — Mesos: one booking per line on screen"):

1. Strict parse: the amount must be closed by ")" ("(+7o))" is no amount).
2. The area's scroll since the last frame is the shift that lines up the
   most messages of the same kind and value, less those it puts on another
   message (messages never change in place; another mesos amount counts
   ``MISREAD``, it may be a misread, while another EXP amount is another kill)
   and ``NEW_ROW`` for each row it makes new at the bottom, each weighted by
   how rare it is in the frame. It
   is measured against the area as last seen: rows unread this frame but read
   in the ``MAP_FRAMES`` before stay where they were, so a few unread rows
   don't make a repeating pattern line up at the wrong shift, and so does a
   row read more than once that reads another number now (a misread must not
   make the next correct read look like a second change). Tracks not read this frame move up with it; when nothing
   lines up (the area was replaced), tracks not read last frame are gone,
   and the others' place is unknown until a read of their value finds them.
3. A row matches a track that has read its value, at or above where the
   track is (messages only scroll up; ``DOWN`` tolerances below, y-noise),
   nearest first, one row per track; but a track read once doesn't take a row
   where another track was read last frame (both reads may be misreads of
   that line). A row left over where a placed track
   should be (within a tolerance) is a read of it whatever it reads: a
   misread of that line, never a new one.
4. A row no track takes is a new line, booked once its value has been read
   ``CONFIRM`` times (a one-off misread never is). Two identical pickups in a
   row are two rows on screen together (the older one scrolled up as the
   newer came in), so one is always left over and booked; if the older one
   isn't read in that frame, the newer is booked once both are read. A track
   unread for ``STALE_S`` while rows show has faded and takes no row at the
   bottom, so it doesn't swallow the next identical pickup.
5. A track's value is the value read most often (ties: the earliest); a
   change of it corrects the booking.
6. A track is gone once unseen for ``GONE_S``, or when the area has been
   empty for ``EMPTY_FRAMES`` frames (faded) and it was first read over
   ``YOUNG_S`` ago (a message stays up longer, so a younger one was only
   unread).
7. A value over ``SPIKE`` times the median of the recent pickups, ending in
   1, 7 or 9, whose last digit dropped lands within 2x of that median, is a
   ")" read as a digit: the digit is dropped. Before ``MIN_RECENT`` pickups
   only the closing ")" of rule 1 guards against it.
8. A line booked for a value that reads a value never read before in it
   ``SPLIT`` times in a row is a new line that came in where it sat (a scroll
   lost: in a short repeating area one frame can't tell a misread from a
   scroll that changed a value in place), if, when those reads began, a mesos
   line (amount readable or not) showed above it where the frame before had
   none: the old line, scrolled up. The new value is its own track;
   the old one's place is unknown until a read of its value finds it.
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
DOWN = 2.0  # tolerances a read of a track's value may sit below it (y-noise)
NEW_ROW = 0.4  # what a shift loses per row it makes new at the bottom (prefers small scrolls)
MISREAD = 0.25  # what a shift loses per mesos row it puts on another mesos amount
SPLIT = 4  # reads in a row of a new value at a line's place that make it a new line there
MAP_FRAMES = 4  # frames an unread row is still taken to be where it was
STALE_S = 1.0  # unread this long while rows show: faded, it takes no new bottom row
_CLOSE = re.compile(r"\s*\)")
_LIKE = re.compile(r"gained\s*m[e3][s5][o0][s5]?", re.IGNORECASE)  # a mesos line, amount or not


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


def _shift(prev: list[tuple[tuple, float, int]], cur: list[tuple[tuple, float]],
           tol: float) -> tuple[float, int]:
    """How far the area scrolled up since ``prev``: the shift that lines up the most rows of the
    same text, less the rows it puts on another text (messages never change in place), each
    weighted by how rare its text is (a repeating kill-and-pickup pattern lines up at several
    shifts). Another mesos amount counts only ``MISREAD`` against (it may be a misread).
    Ties: the smallest. -> (shift, rows lined up)."""
    n = Counter(k for k, _ in cur)
    low = max((py for _, py, _ in prev), default=0.0)
    best = (-math.inf, 0.0, 0.0, 0, [])  # (score, -|shift|, shift, rows lined up, their shifts)
    for d in {round(py - y, 1) for k, y in cur for pk, py, _ in prev if pk == k and py - y >= -tol}:
        used: set[int] = set()
        score, lined, ds = 0.0, 0, []
        for k, y in cur:
            j = next((j for j, (pk, py, _) in enumerate(prev)
                      if j not in used and pk == k and abs(py - d - y) <= tol), None)
            if j is not None:
                used.add(j)
                ds.append(prev[j][1] - y)
                lined += 1
                score += 1 / n[k]
            elif (was := [(pk, pn) for pk, py, pn in prev if abs(py - d - y) <= tol]):
                # Another text where this row was: messages never change in place.
                # Weighted by how rare the text that was there is (a misread is rare itself).
                score -= max((MISREAD if pk[0] == k[0] == "mesos" else 1) / max(1, n[pk]) for pk, _ in was)
            elif y + d > low + tol:
                score -= NEW_ROW / n[k]  # a row new at the bottom: few arrive in one frame
        best = max(best, (score, -abs(d), d, lined, ds), key=lambda b: b[:3])
    return (median(best[4]) if best[4] else best[2]), best[3]  # (the median: y-noise)


class _Track:
    __slots__ = ("votes", "order", "y", "first", "last", "frame", "booked", "for_value", "unread", "placed", "gap", "run", "came")

    def __init__(self, v: int, y: float, t: float, f: int) -> None:
        self.votes: Counter[int] = Counter({v: 1})
        self.order = [v]  # values in the order first read (ties go to the earliest)
        self.y, self.first, self.last, self.frame = y, t, t, f
        self.booked = 0  # mesos booked for it
        self.for_value: int | None = None  # the value read that booking is for
        self.unread: float | None = None  # when rows first showed without it since its last read
        self.gap = False  # an empty frame since its last read: a row there now may be a new line
        self.placed = True  # where it is, is known (not after a scroll nothing lined up for)
        self.run = (v, 1)  # the last value read, and in how many reads in a row
        self.came = False  # when that run began, a mesos line showed above where none was (rule 8)

    @property
    def value(self) -> int:
        best = max(self.votes.values())
        return next(v for v in self.order if self.votes[v] == best)

    def read(self, v: int, y: float, t: float, f: int) -> None:
        if v not in self.votes:
            self.order.append(v)
        self.votes[v] += 1
        self.run = (v, self.run[1] + 1 if v == self.run[0] else 1)
        self.y, self.last, self.frame, self.unread, self.gap = y, t, f, None, False


class MesosLines:
    """Mesos booked per line on screen; ``update`` returns the change to the total."""

    def __init__(self) -> None:
        self._tracks: list[_Track] = []
        self._empty = 0
        self._tol = None  # half a row, from the last frame that showed one
        self._recent: deque[int] = deque(maxlen=RECENT)
        # The area as last seen, as (what it says, y, frame read, frames read in place): the rows of
        # the last frame with messages, plus rows not read in it but read in the ``MAP_FRAMES``
        # before (moved with the scroll), so a few unread rows don't make a repeating pattern line
        # up at the wrong shift.
        self._prev: list[tuple[tuple, float, int, int]] = []
        self._f = 0  # frames read
        self._like: list[float] = []  # where the last frame showed mesos lines (amount or not)

    def _plausible(self, v: int) -> int:
        if len(self._recent) >= MIN_RECENT:
            med = median(self._recent)
            if v % 10 in (1, 7, 9) and v > SPIKE * med and med / 2 <= v // 10 <= 2 * med:
                return v // 10
        return v

    def update(self, lines, now: float) -> int:
        rows = visible_rows(lines)
        hs = sorted(r[2] for r in rows if r[2])
        gaps = [b[1] - a[1] for a, b in zip(rows, rows[1:]) if b[1] > a[1]]
        if hs or gaps:
            self._tol = 0.45 * (hs[len(hs) // 2] if hs else median(g for g in gaps if g <= 1.5 * min(gaps)))
        tol = self._tol if self._tol is not None else 0.45
        self._empty = 0 if rows else self._empty + 1
        faded = self._empty >= EMPTY_FRAMES
        self._tracks = [x for x in self._tracks if now - x.last <= GONE_S
                        and not (faded and now - x.first >= YOUNG_S)]
        self._f += 1
        f = self._f
        cur = [(k, y) for text, y, *_ in rows if (k := _key(text))]
        if cur:
            d, lined = _shift([(k, y, c) for k, y, _, c in self._prev], cur, tol)
            for x in self._tracks:
                x.y -= d  # the area scrolled: lines not read this frame moved up too
                if self._prev and not lined and x.frame != f - 1:
                    x.y = -math.inf  # nothing lines up: the area was replaced, so it is gone
                elif self._prev and not lined and len(cur) > 1:
                    x.placed = False  # it scrolled by we don't know how much: a new line may sit there
            old = [(k, y - d, g, c) for k, y, g, c in self._prev] if lined else []
            self._prev = [(k, y, f, 1 + max((c for pk, py, _, c in old if pk == k and abs(py - y) <= tol),
                                            default=0)) for k, y in cur]
            # (Also rows read more than once that read another number of the same kind now: that
            # may be a misread, so the next frame's read of the old number still lines up.)
            self._prev += [e for e in old if f - e[2] < MAP_FRAMES
                           and all(abs(e[1] - cy) > tol or (ck[0] == e[0][0] and ck != e[0] and e[3] > 1)
                                   for ck, cy in cur)]
        found = [(v, y) for text, y, *_ in rows if (v := parse_mesos(text))]
        bottom = max((r[1] for r in rows), default=None)
        took: dict[int, int] = {}
        used: set[int] = set()

        def take(near) -> None:
            pairs = sorted((abs(x.y - y), i, j)
                           for i, (v, y) in enumerate(found) if i not in took
                           for j, x in enumerate(self._tracks) if j not in used and near(v, y, x)
                           and not (x.unread is not None and now - x.unread >= STALE_S
                                    and abs(y - bottom) <= tol))  # rule 4: a stale line takes no new row
            for _, i, j in pairs:
                if i not in took and j not in used:
                    took[i] = j
                    used.add(j)

        # Rule 3: rows that read a track's value, at or above it (a little below: y-noise) ...
        # A track read once (that read may have been a misread itself) doesn't take a row away
        # from another track read there last frame: that row is a misread of that line.
        def held(y, x) -> bool:
            return x.votes.total() < CONFIRM and abs(x.y - y) > tol and any(o is not x and o.placed and o.frame == f - 1
                                              and abs(o.y - y) <= tol for o in self._tracks)
        take(lambda v, y, x: v == x.value and y <= x.y + DOWN * tol and not held(y, x))
        for j in took.values():
            self._tracks[j].placed = True
        # ... then a row where a track should be is a read of it, whatever it reads (a misread).
        take(lambda v, y, x: x.placed and not x.gap and abs(y - x.y) <= tol)
        like = [y for text, y, *_ in rows if _LIKE.search(text)]
        delta = 0
        for i, (v, y) in enumerate(found):
            if i in took:
                x = self._tracks[took[i]]
                x.read(v, y, now, f)
                if x.run[1] == 1:  # rule 8: did a mesos line come in above it (a scroll)?
                    x.came = any(ly < y - tol and all(abs(ly - py) > tol for py in self._like) for ly in like)
            else:
                self._tracks.append(_Track(v, y, now, f))
        self._like = like
        for x in list(self._tracks):
            v, k = x.run
            if k == SPLIT and x.came and x.votes[v] == k and x.for_value not in (None, v):
                # Rule 8: a new line came in where it sat (a scroll the shift missed): a misread
                # isn't read the same, never seen before, this many times in a row.
                del x.votes[v]
                x.order.remove(v)
                x.placed, x.run = False, (x.value, 0)  # it scrolled up by we don't know how much
                new = _Track(v, x.y, now, f)
                new.votes[v], new.run = k, (v, k)
                self._tracks.append(new)
        for x in self._tracks:
            x.gap = x.gap or not rows
            if rows and x.frame != f and x.unread is None:
                x.unread = now  # the first frame since its last read that showed rows without it
            if x.value == x.for_value or x.votes[x.value] < CONFIRM:
                continue  # a value read once may be a misread: wait for a second read
            v = self._plausible(x.value)
            if x.for_value is None:
                self._recent.append(v)
            delta += v - x.booked
            x.booked, x.for_value = v, x.value
        return delta
