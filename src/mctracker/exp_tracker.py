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


@dataclass(frozen=True)
class Reading:
    t: float  # seconds (monotonic)
    level: int
    exp: int  # EXP into the current level


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

    @property
    def net(self) -> int:
        return self.gained - self.lost

    def add(self, r: Reading) -> int:
        """Record a reading; returns the EXP delta it represents."""
        if self._last is None:
            self._last = r
            if self._start_t is None:
                self._start_t = r.t
            self._history.append((r.t, self.net))
            return 0

        prev = self._last
        if self._pending is not None:
            pending, self._pending = self._pending, None
            if self._confirms(prev, pending, r):
                return self._accept(prev, r, trust=True)
            self.rejected += 1  # the odd reading was a misread; r is judged on its own
        if self._suspicious(prev, r):
            self._pending = r
            return 0
        return self._accept(prev, r)

    def _accept(self, prev: Reading, r: Reading, trust: bool = False) -> int:
        delta = self._delta(prev, r)
        if delta is None:
            if not trust:
                return 0
            # Confirmed but not computable (level went down, or its size is
            # unknown): adopt it as the new baseline rather than staying stuck.
            self._last = r
            self._history.append((r.t, self.net))
            return 0
        if delta >= 0:
            self.gained += delta
        else:
            self.lost += -delta
        self.levels_gained += max(0, r.level - prev.level)
        self._last = r
        self._history.append((r.t, self.net))
        while len(self._history) > 2 and self._history[0][0] < r.t - self.window_s:
            self._history.popleft()
        return delta

    def _confirms(self, prev: Reading, pending: Reading, r: Reading) -> bool:
        """Does ``r`` agree with the odd reading ``pending`` (vs. the baseline)?"""
        if r.level != pending.level:
            return False
        if pending.level != prev.level:
            return True  # two readings in a row on the new level
        return abs(r.exp - pending.exp) <= abs(r.exp - prev.exp)

    def _suspicious(self, prev: Reading, r: Reading) -> bool:
        if r.level != prev.level:
            return True
        need = self.exp_table.get(prev.level)
        if need and r.exp > need:
            return True
        elapsed = (prev.t - self._start_t) if self._start_t is not None else 0
        if elapsed < MIN_RATE_SPAN_S or self.gained <= 0:
            return False  # no rate to compare against yet
        per_min = self.gained / elapsed * 60
        limit = SPIKE_FACTOR * per_min
        if need:
            limit = max(limit, need * MIN_SPIKE_LEVEL_FRACTION)
        return abs(r.exp - prev.exp) > limit

    def reset_baseline(self) -> None:
        """Next reading becomes the new baseline (e.g. after a pause)."""
        self._last = None
        self._pending = None

    def _delta(self, a: Reading, b: Reading) -> int | None:
        if b.level == a.level:
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
