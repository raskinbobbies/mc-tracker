"""EXP tracking from periodic (time, level, exp, percent) readings of the bar.

Readings come from screen recognition, so any field can be misread. The
tracker doesn't trust the level digits: it solves for the level. The bar shows
"exp [pct%]" with pct = round(exp / need(L) * 100, 2), and with the EXP table
only one level usually fits a given (exp, pct) pair. A reading that fits
exactly one level is *validated*; everything else is shown but never moves the
committed level or position.

Gain is derived, not accumulated: a position is sum(need(1..L-1)) + exp, and
the session's gain is the current position minus the start position, plus
confirmed death losses. So a bad reading is corrected by the next good one and
multi-level jumps just work.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Mapping

from .exp_table import EXP_TABLE, bar_percent

log = logging.getLogger(__name__)

# A shown percent fits a level when it's within this of the rounded value.
PCT_TOLERANCE = 0.01
# A same-level drop is a death only if it's at most this share of the level
# (the classic death penalty) and holds for DEATH_CONFIRM readings in a row.
DEATH_MAX_FRACTION = 0.10
DEATH_CONFIRM = 3
# With the percent unreadable, a same-level gain is booked at session end only
# if the last this-many readings all show it.
UNVALIDATED_HOLD = 3
# Learning a level's size from the bar: intersect the need intervals of many
# readings and accept once the interval is this narrow.
LEARN_MIN_READINGS = 10
LEARN_MIN_DISTINCT = 3
LEARN_MAX_WIDTH = 0.002


@dataclass(frozen=True)
class Reading:
    t: float  # seconds (monotonic)
    level: int  # level digits as read (only a tie-breaker)
    exp: int  # EXP into the current level
    pct: float | None = None  # bar percent, None when unreadable


@dataclass
class _Learner:
    lo: float
    hi: float
    n: int = 1
    exps: set[int] = field(default_factory=set)


@dataclass
class _Dip:
    ref: Reading  # committed reading before the drop
    low: int  # lowest EXP seen since
    n: int = 1


@dataclass
class ExpTracker:
    # EXP needed to go from level L to L+1 (trusted, e.g. the shipped table).
    exp_table: Mapping[int, int] = field(default_factory=lambda: EXP_TABLE)
    window_s: float = 600.0
    # Sizes learned from the bar for levels the table lacks or got wrong.
    learned: dict[int, int] = field(default_factory=dict)
    distrusted: set[int] = field(default_factory=set)

    lost: int = field(default=0, init=False)  # confirmed death losses
    rejected: int = field(default=0, init=False)  # readings not validated
    warnings: list[str] = field(default_factory=list, init=False)
    # Set by add() when a reading corrects the previous step: the part of the
    # returned delta that takes that step back (the rest is new gain).
    undone: int = field(default=0, init=False)

    _cur: Reading | None = field(default=None, init=False)  # committed reading
    _prev: Reading | None = field(default=None, init=False)  # committed before it
    _anchor: Reading | None = field(default=None, init=False)  # segment start
    _seg: int = field(default=0, init=False)  # gain from anchor to _cur
    _step: int = field(default=0, init=False)  # delta of the last commit
    _banked: int = field(default=0, init=False)  # gain of closed segments
    _levels_banked: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)
    _start_t: float | None = field(default=None, init=False)
    _start_candidates: list[Reading] = field(default_factory=list, init=False)
    _ambiguous: list[Reading] = field(default_factory=list, init=False)
    _unvalidated: list[Reading] = field(default_factory=list, init=False)
    _dip: _Dip | None = field(default=None, init=False)
    _learners: dict[int, _Learner] = field(default_factory=dict, init=False)
    # (t, cumulative net EXP) for the rolling window
    _history: deque[tuple[float, int]] = field(default_factory=deque, init=False)

    # --- results -----------------------------------------------------------------

    @property
    def net(self) -> int:
        return self._banked + self._seg

    @property
    def gained(self) -> int:
        return self.net + self.lost

    @property
    def levels_gained(self) -> int:
        if self._cur is None or self._anchor is None:
            return self._levels_banked
        return self._levels_banked + self._cur.level - self._anchor.level

    @property
    def last(self) -> Reading | None:
        """The committed (validated) reading."""
        return self._cur

    # --- table -------------------------------------------------------------------

    def need(self, level: int) -> int | None:
        """Size of ``level``: the table's, unless it was found wrong, else learned."""
        if level in self.exp_table and level not in self.distrusted:
            return self.exp_table[level]
        return self.learned.get(level)

    def _trusted(self, level: int) -> bool:
        return level in self.exp_table and level not in self.distrusted

    def fits(self, r: Reading) -> list[int]:
        """Levels whose size explains the reading's (exp, pct)."""
        if r.pct is None:
            return []
        levels = {*self.exp_table, *self.learned}
        out = []
        for lvl in sorted(levels):
            need = self.need(lvl)
            if need and 0 <= r.exp < need and abs(bar_percent(r.exp, need) - r.pct) <= PCT_TOLERANCE + 1e-9:
                out.append(lvl)
        return out

    def _dist(self, a: Reading, b: Reading) -> int | None:
        """EXP from ``a`` to ``b``; None if it would need an untrusted size."""
        if a.level == b.level:
            return b.exp - a.exp
        lo, hi = sorted((a.level, b.level))
        if not all(self._trusted(lvl) for lvl in range(lo, hi)):
            return None
        span = sum(self.exp_table[lvl] for lvl in range(lo, hi))
        if b.level > a.level:
            return span - a.exp + b.exp
        return -(span - b.exp + a.exp)

    # --- input -------------------------------------------------------------------

    def add(self, r: Reading) -> int:
        """Record a reading; returns the change in session gain it causes."""
        if self._start_t is None:
            self._start_t = r.t
        self.undone = 0
        levels = self.fits(r)
        if len(levels) == 1:
            fit = Reading(r.t, levels[0], r.exp, r.pct)
            if self._crossing_ok(fit, r):
                delta = self._validated(fit)
            else:  # only a learned size backs this level change: don't book it
                self.rejected += 1
                self._unvalidated.append(r)
                delta = 0
            self._record(r.t)
            return delta
        self._unvalidated_reading(r, levels)
        self._record(r.t)
        return 0

    def _crossing_ok(self, fit: Reading, raw: Reading) -> bool:
        # Never book a level crossing from a learned size alone.
        if self._trusted(fit.level) or self._cur is None or fit.level == self._cur.level:
            return True
        return raw.level == fit.level

    def _unvalidated_reading(self, r: Reading, levels: list[int]) -> None:
        self.rejected += 1
        if levels:  # several levels fit (EXP near 0%)
            cands = [Reading(r.t, lvl, r.exp, r.pct) for lvl in levels]
            self._ambiguous = cands
        else:
            cands = [r] if (n := self.need(r.level)) and 0 <= r.exp < n else []
            self._unvalidated.append(r)
            if r.pct is not None:
                self._learn(r)
        if not self._started and not self._start_candidates:
            self._start_candidates = cands

    def _validated(self, r: Reading) -> int:
        if self._cur is None:
            return self._begin(r)
        if self._dip is not None:
            dip = self._dip
            if r.level == dip.ref.level and r.exp < dip.ref.exp:
                dip.n += 1
                dip.low = min(dip.low, r.exp)
                if dip.n < DEATH_CONFIRM:
                    return 0
                loss = dip.ref.exp - dip.low
                self.lost += loss
                self._dip = None
                log.info("death at level %d: -%d EXP", r.level, loss)
                return self._commit(r, r.exp - dip.ref.exp) + loss
            self._dip = None  # the drop didn't hold: it was a misread
        d = self._dist(self._cur, r)
        if d is None:
            if r.level < self._cur.level:
                self.rejected += 1  # can't place it below an unknown level
                return 0
            return self._bank_crossing(r)
        if d < 0 and self._death_like(r, -d):
            self._dip = _Dip(self._cur, r.exp)
            return 0
        if d < 0 and self._step > 0 and self._prev is not None:
            back = self._dist(self._prev, r)
            if back is not None and back >= 0:  # the last step was a misread
                self.undone = self._step
        return self._commit(r, d)  # gains, and corrections of a bad reading

    def _death_like(self, r: Reading, drop: int) -> bool:
        cur = self._cur
        need = self.need(r.level)
        if cur is None or r.level != cur.level or not need or drop > need * DEATH_MAX_FRACTION:
            return False
        # Falling back from a spike lands at or above the reading before it;
        # a death drops below both.
        prev = self._prev
        if prev is None:
            return True
        back = self._dist(prev, r)
        return back is not None and back < 0

    def _begin(self, r: Reading) -> int:
        """First validated reading of a segment (session start, or after a pause)."""
        start = r
        if not self._started:
            self._started = True
            best = None
            for c in self._start_candidates:
                d = self._dist(c, r)
                if d is not None and d >= 0 and (best is None or d < best[0]):
                    best = (d, c)  # lowest-gain explanation of the first reading
            if best is not None:
                start = best[1]
            self._start_candidates = []
        self._anchor = self._cur = start
        self._prev = None
        self._seg = 0
        self._clear_tail()
        if start is r:
            return 0
        return self._commit(r, self._dist(start, r) or 0)

    def _bank_crossing(self, r: Reading) -> int:
        """A level-up past a level whose size isn't trusted: keep the EXP made
        so far and the new level's EXP, but not the unknown remainder."""
        assert self._cur is not None and self._anchor is not None
        self._banked += self._seg
        self._levels_banked += r.level - self._anchor.level
        self._anchor = Reading(r.t, r.level, 0, 0.0)
        self._seg = 0
        log.info("level %d -> %d crossed an unknown level size; remainder not booked", self._cur.level, r.level)
        return self._commit(r, r.exp)

    def _commit(self, r: Reading, delta: int) -> int:
        self._prev, self._cur = self._cur, r
        self._seg += delta
        self._step = delta
        self._clear_tail()
        return delta

    def _clear_tail(self) -> None:
        self._ambiguous = []
        self._unvalidated = []

    def _record(self, t: float) -> None:
        self._history.append((t, self.net))
        while len(self._history) > 2 and self._history[0][0] < t - self.window_s:
            self._history.popleft()

    # --- learning ------------------------------------------------------------------

    def _learn(self, r: Reading) -> None:
        """Narrow down the size of the level the digits show, from a reading no
        known level explains."""
        assert r.pct is not None
        lvl = r.level
        if lvl <= 0 or r.pct <= 0:
            return
        lo = max(r.exp + 1, r.exp * 100 / (r.pct + 0.005))
        hi = r.exp * 100 / (r.pct - 0.005) if r.pct > 0.005 else float("inf")
        ln = self._learners.get(lvl)
        if ln is None or max(ln.lo, lo) > min(ln.hi, hi):
            ln = self._learners[lvl] = _Learner(lo, hi)  # (re)start: no overlap
        else:
            ln.lo, ln.hi, ln.n = max(ln.lo, lo), min(ln.hi, hi), ln.n + 1
        ln.exps.add(r.exp)
        if ln.n < LEARN_MIN_READINGS or len(ln.exps) < LEARN_MIN_DISTINCT:
            return
        if ln.hi == float("inf") or (ln.hi - ln.lo) / ln.lo >= LEARN_MAX_WIDTH:
            return
        value = round((ln.lo + ln.hi) / 2)
        del self._learners[lvl]
        if self._trusted(lvl):
            table = self.exp_table[lvl]
            if ln.lo <= table <= ln.hi:
                return
            self.distrusted.add(lvl)
            msg = f"EXP table looks wrong for level {lvl}: the bar implies ~{value:,}, the table says {table:,}"
            self.warnings.append(msg)
            log.warning(msg)
        else:
            log.info("learned EXP for level %d: ~%d", lvl, value)
        self.learned[lvl] = value

    # --- session end / pause ---------------------------------------------------------

    def finalize(self) -> int:
        """Book what the trailing unvalidated readings safely show (session
        ending). Returns the change in gain."""
        cur = self._cur
        if cur is None:
            return 0
        best = None
        for c in self._ambiguous:  # several levels fit: take the lowest gain
            d = self._dist(cur, c)
            if d is not None and d >= 0 and (best is None or d < best[0]):
                best = (d, c)
        if best is not None:
            return self._commit(best[1], best[0])
        tail = self._unvalidated[-UNVALIDATED_HOLD:]
        need = self.need(cur.level)
        if (
            len(tail) == UNVALIDATED_HOLD
            and need
            and all(r.level == cur.level and cur.exp < r.exp < need for r in tail)
        ):
            low = min(tail, key=lambda r: r.exp)
            return self._commit(low, low.exp - cur.exp)
        self._clear_tail()
        return 0

    def reset_baseline(self) -> int:
        """Next validated reading starts a new segment (e.g. after a pause):
        EXP made in between doesn't count. Returns the change booked first."""
        delta = self.finalize()
        if self._cur is not None and self._anchor is not None:
            self._banked += self._seg
            self._levels_banked += self._cur.level - self._anchor.level
        self._seg = 0
        self._cur = self._prev = self._anchor = None
        self._dip = None
        self._clear_tail()
        return delta

    # --- rates ---------------------------------------------------------------------

    def rate_per_hour(self, now: float | None = None) -> float:
        """Net EXP/hr over the rolling window."""
        if len(self._history) < 2:
            return 0.0
        t0, e0 = self._history[0]
        t1, e1 = self._history[-1]
        end = now if now is not None else t1
        dt = end - t0
        return (e1 - e0) / dt * 3600 if dt > 0 else 0.0

    def session_rate_per_hour(self, now: float | None = None) -> float:
        if self._start_t is None or not self._history:
            return 0.0
        end = now if now is not None else self._history[-1][0]
        dt = end - self._start_t
        return self.net / dt * 3600 if dt > 0 else 0.0

    def exp_remaining(self) -> int | None:
        """EXP left to the next level, or None if the level's size isn't known."""
        if self._cur is None:
            return None
        need = self.need(self._cur.level)
        if need is None:
            return None
        left = need - self._cur.exp
        return left if left > 0 else None

    def seconds_to_level(self, now: float | None = None) -> float | None:
        """Time to the next level at the session-average rate, falling back to
        the rolling-window rate. None when there is no usable rate or size."""
        left = self.exp_remaining()
        if left is None:
            return None
        rate = self.session_rate_per_hour(now)
        if rate <= 0:
            rate = self.rate_per_hour(now)
        if rate <= 0:
            return None
        return left / rate * 3600
