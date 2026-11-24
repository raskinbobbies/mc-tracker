"""EXP/hr tracking from periodic (time, level, exp) readings.

Readings come from screen recognition, so the tracker tolerates gaps and
handles level-ups (EXP resets) and deaths (EXP loss).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping


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
    # Consecutive readings skipped as implausible (e.g. a level-up with no EXP
    # table entry): while > 0, ``gained`` is behind the real bar.
    rejected: int = field(default=0, init=False)
    # (t, cumulative net EXP) for the rolling window
    _history: deque[tuple[float, int]] = field(default_factory=deque, init=False)

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
        delta = self._delta(prev, r)
        if delta is None:  # implausible (e.g. misread level); skip it
            self.rejected += 1
            return 0
        self.rejected = 0

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

    @property
    def has_baseline(self) -> bool:
        return self._last is not None

    def reset_baseline(self) -> None:
        """Next reading becomes the new baseline (e.g. after a pause)."""
        self._last = None

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

    def seconds_to_level(self, now: float | None = None) -> float | None:
        if self._last is None:
            return None
        need = self.exp_table.get(self._last.level)
        rate = self.rate_per_hour(now)
        if need is None or rate <= 0:
            return None
        return (need - self._last.exp) / rate * 3600
