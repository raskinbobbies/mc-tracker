"""Aggregated state for one grinding session."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .exp_tracker import ExpTracker, Reading
from .kills import KillCounter, LineTracker, Mob, estimate_missed, parse_message


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
    message_exp: int = 0  # EXP total (incl. party bonus) from messages we read
    party_bonus_exp: int = 0
    loot_unknown: Counter[str] = field(default_factory=Counter)  # names we couldn't map to an id
    item_resolver: Callable[[str], int | None] | None = None

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
        # The bar shows "exp [pct%]", so EXP needed for this level = exp / pct.
        # Only trust it when pct is large enough for 2-decimal rounding to be small.
        if percent >= 5 and level not in self.exp.exp_table:
            self.exp.exp_table = {**self.exp.exp_table, level: round(exp * 100 / percent)}
        self.exp.add(Reading(t if t is not None else time.monotonic(), level, exp))

    def on_message_lines(self, visible_lines) -> None:
        """Feed the message area's visible lines each frame.

        Accepts strings (oldest -> newest) or objects/tuples with text and y.
        """
        for line in self.lines.update(visible_lines):
            msg = parse_message(line)
            if msg is None:
                continue
            if msg.kind == "exp":
                self.killer.add(msg.value)
                self.message_exp += msg.value
            elif msg.kind == "party_bonus":
                self.killer.mark_last_party()
                self.message_exp += msg.value
                self.party_bonus_exp += msg.value
            elif msg.kind == "loot":
                item_id = self.item_resolver(msg.item) if self.item_resolver else None
                if item_id is None:
                    self.loot_unknown[msg.item] += msg.value
                else:
                    self.on_loot(item_id, msg.item, msg.value)

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
