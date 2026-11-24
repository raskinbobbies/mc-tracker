"""EXP/hr tracking from periodic (time, level, exp) readings.

Readings come from screen recognition, so the tracker tolerates gaps and
handles level-ups (EXP resets) and deaths (EXP loss).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping


# A same-level EXP jump bigger than SPIKE_FACTOR times the session's average
# per-minute rate (and than a small slice of the level) must be seen on two
# readings in a row before it counts. Guards against single OCR misreads.
SPIKE_FACTOR = 20
MIN_SPIKE_LEVEL_FRACTION = 0.01
MIN_RATE_SPAN_S = 60.0
# Levels never go down in this game (death costs EXP, not levels), and they go
# up one at a time from a nearly full bar. Any other level change is taken as a
# misread unless it holds for this many readings in a row.
LEVEL_HOLD_READINGS = 30
# A +1 level change is plausible when the bar was at least this full before it.
LEVEL_UP_NEAR_FULL = 0.75


@dataclass(frozen=True)
class Reading:
    t: float  # seconds (monotonic)
    level: int
    exp: int  # EXP into the current level
    pct: float | None = None  # bar percent, when known


@dataclass
class ExpTracker:
    # EXP required to go from level N to N+1. Needed to account for level-ups.
    exp_table: Mapping[int, int] = field(default_factory=dict)
    window_s: float = 600.0

    _last: Reading | None = field(default=None, init=False)
    _start_t: float | None = field(default=None, init=False)
    gained: int = field(default=0, init=False)
    lost: int = field(default=0, init=False)
    levels_gained: int = field(default=0, init=False)
    # (t, cumulative net EXP) for the rolling window
    _history: deque[tuple[float, int]] = field(default_factory=deque, init=False)
    # A reading that looks wrong (level change, or an EXP jump far beyond the
    # recent rate) waits here until the next reading confirms or refutes it.
    _pending: Reading | None = field(default=None, init=False)
    rejected: int = field(default=0, init=False)  # readings dropped as misreads
    # Implausible level readings in a row, as (level, count). Adopted only
    # once they've held for LEVEL_HOLD_READINGS.
    _hold: tuple[int, int] | None = field(default=None, init=False)
    # The level we were on before adopting a long-held lower level. Coming
    # back to it ends the misread, so it's booked as a same-level change.
    _pre_drop_level: int | None = field(default=None, init=False)

    @property
    def net(self) -> int:
        return self.gained - self.lost

    @property
    def last(self) -> Reading | None:
        """The latest accepted reading (the baseline)."""
        return self._last

    def add(self, r: Reading) -> int:
        """Record a reading; returns the EXP delta it represents."""
        if self._last is None:
            if self._start_t is None:
                self._start_t = r.t
            self._set_baseline(r)
            return 0

        prev = self._last
        if r.level != prev.level and not self._level_plausible(prev, r):
            return self._held(prev, r)
        self._hold = None
        if self._pending is not None:
            pending, self._pending = self._pending, None
            if self._confirms(prev, pending, r):
                return self._accept(prev, r, trust=True)
            self.rejected += 1  # the odd reading was a misread; r is judged on its own
        if self._suspicious(prev, r):
            self._pending = r
            return 0
        return self._accept(prev, r)

    def flush(self) -> int:
        """Accept the reading still waiting for confirmation, if plausible.

        Called when a session ends, so a level-up or big gain seen on the very
        last reading isn't lost. Returns the EXP delta booked.
        """
        pending, self._pending = self._pending, None
        if pending is None or self._last is None:
            return 0
        prev = self._last
        if pending.level == prev.level:
            # A same-level reading only waits when it looks like a spike; take
            # it only if it's a gain that fits the level and isn't wildly off.
            need = self.exp_table.get(prev.level)
            gain = pending.exp - prev.exp
            limit = self._spike_limit(prev)
            if gain < 0 or (need and pending.exp > need) or (limit is not None and gain > 2 * limit):
                self.rejected += 1
                return 0
        return self._accept(prev, pending, trust=True)

    def _set_baseline(self, r: Reading) -> None:
        self._last = r
        self._history.append((r.t, self.net))
        while len(self._history) > 2 and self._history[0][0] < r.t - self.window_s:
            self._history.popleft()
        if self._pre_drop_level is not None and r.level >= self._pre_drop_level:
            self._pre_drop_level = None

    def _held(self, prev: Reading, r: Reading) -> int:
        """An implausible level reading: drop it unless it keeps holding."""
        if self._pending is not None:
            self._pending = None
            self.rejected += 1
        n = self._hold[1] + 1 if self._hold and self._hold[0] == r.level else 1
        self._hold = (r.level, n)
        self.rejected += 1
        if n < LEVEL_HOLD_READINGS:
            return 0
        # Held long enough to adopt (so a bad baseline can't leave us stuck).
        self._hold = None
        if r.level < prev.level:
            # Keep the old level so returning to it books ~nothing.
            self._pre_drop_level = max(prev.level, self._pre_drop_level or 0)
            self._set_baseline(r)
            return 0
        if r.level == prev.level + 1:  # a real level-up seen from a sparse bar
            return self._accept(prev, r, trust=True)
        self._set_baseline(r)  # multi-level jump: likely the old baseline was wrong
        return 0

    def _level_plausible(self, prev: Reading, r: Reading) -> bool:
        if r.level == self._pre_drop_level:
            return True  # back from a level misread
        if r.level != prev.level + 1:
            return False  # down, or up by more than one
        need = self.exp_table.get(prev.level)
        if need:
            fill = prev.exp / need
        elif prev.pct is not None:
            fill = prev.pct / 100
        else:
            return True  # nothing to judge by
        return fill >= LEVEL_UP_NEAR_FULL

    def _accept(self, prev: Reading, r: Reading, trust: bool = False) -> int:
        delta = self._delta(prev, r)
        if delta is None:
            if not trust:
                return 0
            # Confirmed level-up of unknown size: book the new level's EXP and,
            # if the last percent lets us size the old level, its remainder.
            delta = r.exp
            if prev.pct is not None and prev.pct >= 5:
                need = round(prev.exp * 100 / prev.pct)
                delta += max(0, need - prev.exp)
        if delta >= 0:
            self.gained += delta
        else:
            self.lost += -delta
        if r.level != self._pre_drop_level:
            self.levels_gained += max(0, r.level - prev.level)
        self._set_baseline(r)
        return delta

    def _confirms(self, prev: Reading, pending: Reading, r: Reading) -> bool:
        """Does ``r`` agree with the odd reading ``pending`` (vs. the baseline)?"""
        if r.level != pending.level:
            return False
        if pending.level != prev.level:
            return True  # two readings in a row on the new level
        return abs(r.exp - pending.exp) <= abs(r.exp - prev.exp)

    def _spike_limit(self, prev: Reading) -> float | None:
        """Largest same-level EXP change that isn't a spike; None while there
        is no rate to compare against yet."""
        elapsed = (prev.t - self._start_t) if self._start_t is not None else 0
        if elapsed < MIN_RATE_SPAN_S or self.gained <= 0:
            return None
        limit = SPIKE_FACTOR * self.gained / elapsed * 60
        need = self.exp_table.get(prev.level)
        if need:
            limit = max(limit, need * MIN_SPIKE_LEVEL_FRACTION)
        return limit

    def _suspicious(self, prev: Reading, r: Reading) -> bool:
        if r.level != prev.level:
            return True
        need = self.exp_table.get(prev.level)
        if need and r.exp > need:
            return True
        limit = self._spike_limit(prev)
        return limit is not None and abs(r.exp - prev.exp) > limit

    def reset_baseline(self) -> int:
        """Next reading becomes the new baseline (e.g. after a pause).

        A reading still waiting for confirmation is flushed first; returns the
        EXP delta that booked.
        """
        delta = self.flush()
        self._last = None
        self._hold = None
        self._pre_drop_level = None
        return delta

    def _delta(self, a: Reading, b: Reading) -> int | None:
        if b.level == a.level or b.level == self._pre_drop_level:
            # Same level, or back on the level a long misread hid: the EXP
            # field was right all along.
            return b.exp - a.exp
        if b.level < a.level:
            return None
        # Level-up(s): finish a's level, add full intermediate levels, then b.exp
        total = 0
        for lvl in range(a.level, b.level):
            need = self.exp_table.get(lvl)
            if need is None:
                return None
            total += need
        return total - a.exp + b.exp

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
        if self._start_t is None or self._last is None:
            return 0.0
        end = now if now is not None else self._last.t
        dt = end - self._start_t
        return self.net / dt * 3600 if dt > 0 else 0.0

    def exp_remaining(self) -> int | None:
        """EXP left to the next level, or None if the level's size isn't known
        yet (or the reading says we're already past it)."""
        if self._last is None:
            return None
        need = self.exp_table.get(self._last.level)
        if need is None:
            return None
        left = need - self._last.exp
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
