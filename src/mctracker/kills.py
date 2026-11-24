"""Infer party kills from EXP gain messages.

Every monster your party kills on your map gives you an EXP message, whoever
killed it. So: count new EXP lines -> kills, and identify each line's monster
by matching its EXP against the current map's monsters.

Your share varies (party size, levels, damage split, EXP events), so instead
of an exact formula we fit one multiplicative scale ``k`` such that each
observed value lands near ``k * base_exp`` of some monster on the map, then
assign each value to the nearest one in log space.

The exact message wording is unconfirmed until we have screenshots, so the
parser regex is configurable.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

# Placeholder until we see the real text; matches e.g. "You have gained experience (+1,234)".
DEFAULT_EXP_RE = re.compile(r"experience.*?\+\s*([\d,]+)", re.IGNORECASE)


def parse_exp_line(text: str, pattern: re.Pattern[str] = DEFAULT_EXP_RE) -> int | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


class LineTracker:
    """Detect newly appeared lines in a scrolling message area.

    Each frame gives the visible lines oldest -> newest. New lines push old
    ones up, so the new frame is ``prev[shift:] + new_lines``. We pick the
    smallest shift consistent with both frames (conservative: when identical
    lines make it ambiguous, we under-count; the EXP bar check catches that).
    """

    def __init__(self) -> None:
        self.prev: list[str] = []

    def update(self, lines: Sequence[str]) -> list[str]:
        cur = list(lines)
        prev = self.prev
        self.prev = cur
        for shift in range(len(prev) + 1):
            overlap = prev[shift:]
            if overlap == cur[: len(overlap)]:
                return cur[len(overlap):]
        return cur  # no overlap at all: everything is new


@dataclass(frozen=True)
class Mob:
    id: int
    name: str
    exp: int


@dataclass
class KillCounter:
    mobs: list[Mob] = field(default_factory=list)
    # Max observations used to fit the scale (recent ones matter most).
    fit_window: int = 200
    # Values further than this from any monster (in log units) are "unknown".
    max_log_error: float = math.log(1.6)

    values: list[int] = field(default_factory=list, init=False)
    scale: float = field(default=1.0, init=False)
    _dirty: bool = field(default=False, init=False)

    def set_map(self, mobs: Sequence[Mob]) -> None:
        """Start a new map segment (counts so far are kept by the caller)."""
        self.mobs = [m for m in mobs if m.exp > 0]
        self.values.clear()
        self.scale = 1.0
        self._dirty = False

    def add(self, exp_value: int) -> None:
        if exp_value > 0:
            self.values.append(exp_value)
            self._dirty = True

    @property
    def total(self) -> int:
        return len(self.values)

    def _fit(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        obs = self.values[-self.fit_window:]
        if not obs or not self.mobs:
            return
        logs_b = [math.log(m.exp) for m in self.mobs]
        logs_o = [math.log(v) for v in obs]
        cap = self.max_log_error
        # Candidate scales: every (observation, monster) pairing.
        cands = {round(lo - lb, 4) for lo in set(logs_o) for lb in logs_b}
        best, best_cost = 0.0, float("inf")
        for lk in cands:
            cost = 0.0
            for lo in logs_o:
                cost += min(cap, min(abs(lo - lk - lb) for lb in logs_b))
            # Prefer scales near 1x on ties (solo / no bonus is most common).
            cost += 1e-6 * abs(lk)
            if cost < best_cost:
                best, best_cost = lk, cost
        self.scale = math.exp(best)

    def classify(self, exp_value: int) -> Mob | None:
        self._fit()
        if not self.mobs or exp_value <= 0:
            return None
        lo = math.log(exp_value) - math.log(self.scale)
        mob = min(self.mobs, key=lambda m: abs(lo - math.log(m.exp)))
        return mob if abs(lo - math.log(mob.exp)) <= self.max_log_error else None

    def counts(self) -> Counter[int | None]:
        """Kills per mob id for this map segment (None = unidentified)."""
        self._fit()
        return Counter((m.id if (m := self.classify(v)) else None) for v in self.values)


def coverage(message_sum: int, bar_delta: int) -> float | None:
    """Fraction of EXP-bar gain explained by messages we read (1.0 = nothing missed)."""
    if bar_delta <= 0:
        return None
    return message_sum / bar_delta


def estimate_missed(message_sum: int, bar_delta: int, kills: int) -> int:
    """Kills probably missed (lines scrolled away / ambiguous duplicates)."""
    if kills == 0 or bar_delta <= message_sum:
        return 0
    avg = message_sum / kills
    return round((bar_delta - message_sum) / avg)
