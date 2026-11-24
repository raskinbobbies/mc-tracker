"""Aggregated state for one grinding session."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

from .exp_tracker import ExpTracker, Reading


@dataclass
class Session:
    exp: ExpTracker = field(default_factory=ExpTracker)
    map_name: str = ""
    level: int = 0
    exp_percent: float = 0.0
    mesos: int = 0
    kills: Counter[int] = field(default_factory=Counter)  # mob id -> count
    loot: Counter[int] = field(default_factory=Counter)  # item id -> count
    used: Counter[int] = field(default_factory=Counter)  # item id -> count
    names: dict[tuple[str, int], str] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def on_exp(self, level: int, exp: int, percent: float, t: float | None = None) -> None:
        self.level, self.exp_percent = level, percent
        self.exp.add(Reading(t if t is not None else time.monotonic(), level, exp))

    def on_kill(self, mob_id: int, name: str = "", n: int = 1) -> None:
        self.kills[mob_id] += n
        if name:
            self.names[("mob", mob_id)] = name

    def on_loot(self, item_id: int, name: str = "", n: int = 1) -> None:
        self.loot[item_id] += n
        if name:
            self.names[("item", item_id)] = name

    def on_use(self, item_id: int, name: str = "", n: int = 1) -> None:
        self.used[item_id] += n
        if name:
            self.names[("item", item_id)] = name

    def on_mesos(self, amount: int) -> None:
        self.mesos += amount

    def elapsed(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.started
