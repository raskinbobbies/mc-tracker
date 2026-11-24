"""Aggregated state for one grinding session."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from .exp_tracker import ExpTracker, Reading
from .kills import KillCounter, LineTracker, Mob, estimate_missed, parse_exp_line


@dataclass
class Session:
    exp: ExpTracker = field(default_factory=ExpTracker)
    map_name: str = ""
    level: int = 0
    exp_percent: float = 0.0
    mesos: int = 0
    loot: Counter[int] = field(default_factory=Counter)  # item id -> count
    used: Counter[int] = field(default_factory=Counter)  # item id -> count
    names: dict[tuple[str, int], str] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    # Party kill inference from EXP messages
    killer: KillCounter = field(default_factory=KillCounter)
    lines: LineTracker = field(default_factory=LineTracker)
    _past_kills: Counter[int | None] = field(default_factory=Counter)  # earlier maps
    message_exp: int = 0  # EXP total from messages we read

    # --- inputs from screen recognition -------------------------------------

    def on_map(self, name: str, mobs: Sequence[Mob]) -> None:
        if name == self.map_name:
            return
        self._past_kills += self.killer.counts()
        self.killer.set_map(mobs)
        self.map_name = name
        for m in mobs:
            self.names[("mob", m.id)] = m.name

    def on_exp(self, level: int, exp: int, percent: float, t: float | None = None) -> None:
        self.level, self.exp_percent = level, percent
        self.exp.add(Reading(t if t is not None else time.monotonic(), level, exp))

    def on_message_lines(self, visible_lines: Sequence[str]) -> None:
        """Feed the message area's visible lines (oldest -> newest) each frame."""
        for line in self.lines.update(visible_lines):
            value = parse_exp_line(line)
            if value is not None:
                self.killer.add(value)
                self.message_exp += value

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

    # --- derived -------------------------------------------------------------

    @property
    def kills(self) -> Counter[int]:
        """Party kills per mob id (identified only)."""
        c = self._past_kills + self.killer.counts()
        c.pop(None, None)
        return c

    @property
    def total_kills(self) -> int:
        return sum(self._past_kills.values()) + self.killer.total

    @property
    def missed_kills(self) -> int:
        return estimate_missed(self.message_exp, self.exp.gained, self.total_kills)

    def elapsed(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.started
