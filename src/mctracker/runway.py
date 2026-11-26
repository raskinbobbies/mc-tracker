"""Potion runway: how long the potions on the quickslots last at the current use rate.

Per item assigned in "Items & prices", ``RunwayWatch`` keeps the stack counts read over the last few
minutes (``WINDOW_S``) and estimates ``count / uses per minute``. The estimate is hidden (None) when
the count can't be read now or nothing was used in the window.

Readings are filtered before they count. A plausible step is accepted at once: no change, a drop of at
most ``items.K`` per second (uses), or a rise of at most ``K`` (a loot pickup). Anything else (a misread
such as 142 read as 42, a moved stack, a refill) is held as suspect and only accepted once it has held
for ``SETTLE_S``; a jump straight back drops it. Accepted big steps are never uses. A gap of over
``GAP_S`` between readings (loading, the game hidden) restarts the rate instead of booking the drop.

Accepted limit: the rate is a plain average over the window, so right after a burst (a boss, then
standing in town) the runway reads short until the burst leaves the window (``WINDOW_S``).

The alert fires once per crossing: an estimate under the threshold for ``ALERT_HOLD_S`` (one odd
reading never toasts), and only while the caller says the user can see it (``ready``); otherwise it waits
and fires when ready, if the runway is still short. It re-arms once the estimate is back above the
threshold plus a margin (``rearm_min``), e.g. after a refill. Pure logic with an injectable clock.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .items import K

WINDOW_S = 180.0  # recent use: the last 3 minutes
MIN_SPAN_S = 60.0  # the rate is never taken over less than a minute (one sip isn't a rate)
GAP_S = 10.0  # longer without a reading: start the rate again
SETTLE_S = 5.0  # an implausible step must hold this long to be believed (outlasts most misreads)
ALERT_HOLD_S = 6.0  # the runway must stay short this long before the alert (longer than SETTLE_S)


def rearm_min(threshold_min: float) -> float:
    """Runway that re-arms the alert: well above the threshold, so a wobbling rate doesn't toast twice."""
    return max(threshold_min * 1.5, threshold_min + 2)


def _plausible(step: int, dt: float) -> bool:
    return -K * max(1.0, dt) <= step <= K


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
    # Accepted (t, count, use): ``use`` False for a settled step (held, then accepted), which is never uses.
    samples: deque = field(default_factory=deque)
    suspect: tuple[float, float, int] | None = None  # (first seen, last seen, last count) of an implausible step
    armed: bool = True
    short_since: float | None = None  # when the runway went under the threshold

    last_read: float | None = None  # any reading, accepted or suspect

    def read(self, now: float, count: int) -> None:
        last, self.last_read = self.last_read, now
        if not self.samples or last is None or now - last > GAP_S:
            self.samples.clear()  # first reading, or after a gap: no drop across it is uses
            self.samples.append((now, count, False))
            self.suspect = None
            return
        # The pace allowed is from the last reading, not the last accepted one: a step held as suspect
        # for a few seconds must not turn plausible just because time passed.
        if _plausible(count - self.samples[-1][1], now - last):
            self.samples.append((now, count, True))
            self.suspect = None
        elif self.suspect and _plausible(count - self.suspect[2], now - self.suspect[1]):
            first = self.suspect[0]
            self.suspect = (first, now, count)
            if now - first >= SETTLE_S:  # it held: a moved stack or a refill, not a misread
                self.samples.append((now, count, False))  # its step is never uses, however small
                self.suspect = None
        else:
            self.suspect = (now, now, count)


class RunwayWatch:
    """Recent quickslot counts per assigned item, the runway they give, and when to alert."""

    def __init__(self, window_s: float = WINDOW_S, clock: Callable[[], float] = time.monotonic):
        self.window_s = window_s
        self.clock = clock
        self._items: dict[str, _Item] = {}
        self._last: list[Runway] = []

    def reset(self) -> None:
        """Forget the readings (the game closed or detached); alert states stay."""
        for it in self._items.values():
            it.samples.clear()
            it.suspect = it.short_since = it.last_read = None
        self._last = []

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
            if counts[key] is not None:
                it.read(now, counts[key])
            while len(it.samples) > 1 and it.samples[0][0] < now - self.window_s:
                it.samples.popleft()
            # The accepted count, and only while the slot reads now (a suspect reading shows the last good one).
            count = it.samples[-1][1] if counts[key] is not None and it.samples else None
            per_min = self._rate(it.samples, now)
            minutes = count / per_min if count is not None and per_min > 0 else None
            out.append(Runway(key, name, count, per_min, minutes))
        self._last = out
        return out

    @staticmethod
    def _rate(samples: deque, now: float) -> float:
        uses = 0
        for (_, c0, _), (_, c1, use) in zip(samples, list(samples)[1:]):
            drop = c0 - c1
            if use and drop > 0:  # a settled step (a moved stack) is not uses
                uses += drop
        if not uses:
            return 0.0
        return uses * 60.0 / max(MIN_SPAN_S, now - samples[0][0])

    def alerts(self, threshold_min: float, ready: bool = True) -> list[Runway]:
        """Items to alert for now: under ``threshold_min`` for ``ALERT_HOLD_S``, armed, and ``ready``
        (the user can see it; else it waits). Each fires once, until the runway is back above ``rearm_min``."""
        now = self.clock()
        due = []
        for r in self._last:
            it = self._items.get(r.key)
            if it is None or r.minutes is None:
                if it is not None:
                    it.short_since = None
                continue
            if not it.armed:
                it.armed = r.minutes >= rearm_min(threshold_min)
                continue
            if r.minutes >= threshold_min:
                it.short_since = None
                continue
            if it.short_since is None:
                it.short_since = now
            if ready and now - it.short_since >= ALERT_HOLD_S:
                it.armed, it.short_since = False, None
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
