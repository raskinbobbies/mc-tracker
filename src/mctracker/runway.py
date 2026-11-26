"""Potion runway: how long the potions on the quickslots last at the current use rate.

Per item assigned in "Items & prices", ``RunwayWatch`` keeps the stack counts read over the last few
minutes (``WINDOW_S``) and estimates ``count / uses per minute``. Uses are the drops between readings;
rises (a refill, loot) are not uses, and a drop faster than ``items.K`` per second is a misread or a
moved stack, never uses. The estimate is hidden (None) when the count can't be read now or nothing
was used in the window.

The alert fires once per crossing: when an item's estimate first drops below the threshold, it is
disarmed until a refill (the count rises more than ``items.K`` above its lowest since the alert:
smaller rises are loot pickups). Pure logic with an injectable clock, so it tests without Qt.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .items import K

WINDOW_S = 300.0  # recent use: the last 5 minutes
MIN_SPAN_S = 60.0  # the rate is never taken over less than a minute (one sip isn't a rate)
REFILL = K + 1  # a rise this big after an alert is a refill and re-arms it


@dataclass
class Runway:
    key: str  # ``SlotAssignment.key``
    name: str
    count: int | None  # None: not readable now
    per_min: float  # uses per minute over the window
    minutes: float | None  # None: no estimate (count unreadable or no recent use)


@dataclass
class _Item:
    name: str
    samples: deque = field(default_factory=deque)  # (t, count), oldest first, within the window
    armed: bool = True
    low: int | None = None  # lowest count since the alert


class RunwayWatch:
    """Recent quickslot counts per assigned item, the runway they give, and when to alert."""

    def __init__(self, window_s: float = WINDOW_S, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._items: dict[str, _Item] = {}
        self._last: list[Runway] = []

    def update(self, reads: Sequence, slot_items) -> list[Runway]:
        """One look at the quickslots: ``reads`` are ``SlotRead``-likes (``count``, ``signature``) per slot,
        ``slot_items`` a ``SlotItems``. Returns the runway of each assigned item, in slot order."""
        now = self.clock()
        counts: dict[str, int | None] = {}
        names: dict[str, str] = {}
        for slot in sorted(slot_items.slots):
            a = slot_items.slots[slot]
            names.setdefault(a.key, a.name)
            r = reads[slot] if slot < len(reads) else None
            count = getattr(r, "count", None) if r is not None and a.matches(getattr(r, "signature", None)) else None
            if counts.get(a.key) is None:  # the item on two slots: the first one that reads
                counts[a.key] = count
        for key in list(self._items):
            if key not in names:
                del self._items[key]  # unassigned: forget it
        out = []
        for key, name in names.items():
            it = self._items.setdefault(key, _Item(name))
            it.name = name
            count = counts[key]
            if count is not None:
                it.samples.append((now, count))
                if not it.armed:
                    it.low = count if it.low is None else min(it.low, count)
                    if count >= it.low + REFILL:
                        it.armed, it.low = True, None
            while it.samples and it.samples[0][0] < now - self.window_s:
                it.samples.popleft()
            per_min = self._rate(it.samples, now)
            minutes = count / per_min if count is not None and per_min > 0 else None
            out.append(Runway(key, name, count, per_min, minutes))
        self._last = out
        return out

    @staticmethod
    def _rate(samples: deque, now: float) -> float:
        uses = 0
        for (t0, c0), (t1, c1) in zip(samples, list(samples)[1:]):
            drop = c0 - c1
            if 0 < drop <= K * max(1.0, t1 - t0):
                uses += drop
        if not uses:
            return 0.0
        return uses * 60.0 / max(MIN_SPAN_S, now - samples[0][0])

    def alerts(self, threshold_min: float) -> list[Runway]:
        """Items whose runway just dropped below ``threshold_min``: each once, until a refill re-arms it."""
        due = []
        for r in self._last:
            it = self._items.get(r.key)
            if it is not None and it.armed and r.minutes is not None and r.minutes < threshold_min:
                it.armed, it.low = False, r.count
                due.append(r)
        return due


def fmt_minutes(m: float) -> str:
    if m < 1:
        return "<1 min"
    if m < 100:
        return f"~{round(m)} min"
    return f"~{m / 60:.1f} h"


def runway_text(runways: Sequence[Runway]) -> str:
    """"Elixir 142 · ~18 min   Orange Potion 30 · ~4 min" for the items with an estimate; "" if none."""
    return "   ".join(f"{r.name} {r.count:,} · {fmt_minutes(r.minutes)}" for r in runways if r.minutes is not None)


def alert_text(r: Runway) -> str:
    return f"{r.name} running low: {fmt_minutes(r.minutes)} left ({r.count:,})"
