"""Read saved sessions (JSON files written by ``Tracker.end_session``)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def total_used(*counts) -> int:
    """Items used: the sum over every count iterable (item-id and name-only alike).

    The one rule behind both the live overlay figure and the history POTIONS column.
    """
    return sum(sum(c) for c in counts)


@dataclass
class Entry:
    id: int | None
    name: str
    count: int


@dataclass
class SavedSession:
    path: Path
    data: dict

    @property
    def started(self) -> datetime:
        try:
            return datetime.fromisoformat(self.data["started_at"])
        except (KeyError, TypeError, ValueError):
            return datetime.fromtimestamp(self.path.stat().st_mtime)

    @property
    def day(self) -> str:
        return self.started.strftime("%Y-%m-%d")

    @property
    def map(self) -> str:
        return self.data.get("map") or "Unknown map"

    @property
    def seconds(self) -> int:
        return int(self.data.get("active_seconds", 0))

    @property
    def exp(self) -> int:
        return int(self.data.get("exp_gained", 0))

    @property
    def exp_per_min(self) -> float:
        return self.exp / self.seconds * 60 if self.seconds else 0.0

    @property
    def kills(self) -> int:
        return int(self.data.get("party_kills", 0))

    @property
    def levels(self) -> int:
        return int(self.data.get("levels_gained", 0))

    @property
    def mesos(self) -> int:
        """Mesos picked up."""
        return int(self.data.get("mesos", 0))

    @property
    def mesos_spent(self) -> int:
        return int(self.data.get("mesos_spent", 0))

    @property
    def net_mesos(self) -> int:
        return int(self.data.get("net_mesos", self.mesos - self.mesos_spent))

    @property
    def net_mesos_per_min(self) -> float:
        return self.net_mesos / self.seconds * 60 if self.seconds else 0.0

    @property
    def has_mesos(self) -> bool:
        return "mesos" in self.data or "net_mesos" in self.data

    @property
    def has_items_used(self) -> bool:
        """Saved with items-used data (mesos spent is tracked too). Older files have none."""
        return bool(self.data.get("items_used_estimate")) or "used" in self.data or "used_list" in self.data

    @property
    def potions(self) -> int | None:
        """Items used (consumables, cheese included), or None when the file has no items-used data.

        Files saved before items used were always tracked have none: that is "no data", not zero.
        (Files from the old experimental setting carry ``items_used_estimate``; they still count.) Counts id'd and name-only
        entries alike, via ``total_used`` (shared with the live ``overlay.potions_used``).
        """
        if not self.has_items_used:
            return None
        return total_used(e.count for e in self.entries("used"))

    @property
    def potions_per_min(self) -> float | None:
        p = self.potions
        if p is None:
            return None
        return p / self.seconds * 60 if self.seconds else 0.0

    @property
    def street_and_map(self) -> tuple[str, str]:
        """("Street", "Map") from the saved "Street : Map" name; the street is "" when there is none."""
        street, sep, name = self.map.partition(" : ")
        return (street.strip(), name.strip()) if sep and name.strip() else ("", self.map)

    @property
    def exp_per_minute(self) -> list[int] | None:
        """EXP per active minute (the last entry is the partial final minute), or None for old files."""
        raw = self.data.get("exp_per_minute")
        if not isinstance(raw, list) or not raw:
            return None
        try:
            return [int(v) for v in raw]
        except (TypeError, ValueError):
            return None

    @property
    def best_minute(self) -> tuple[int, int] | None:
        """(minute index, EXP) of the best minute; None when there is no per-minute data or it is all zero."""
        mins = self.exp_per_minute
        if not mins or max(mins) <= 0:
            return None
        return mins.index(max(mins)), max(mins)

    @property
    def level_end(self) -> int | None:
        lvl = self.data.get("level")
        return int(lvl) if isinstance(lvl, (int, float)) and lvl > 0 else None

    @property
    def level_span(self) -> tuple[int, float, int, float, float] | None:
        """(start level, start %, end level, end %, % gained) or None when the file lacks level data.

        The % gained counts whole levels: 80% at level 50 to 10% at level 51 is 30%.
        """
        d = self.data
        end, p0, p1 = self.level_end, d.get("exp_percent_start"), d.get("exp_percent")
        if end is None or not isinstance(p0, (int, float)) or not isinstance(p1, (int, float)):
            return None
        start = d.get("level_start")
        start = int(start) if isinstance(start, (int, float)) and start > 0 else end - self.levels
        return start, float(p0), end, float(p1), (end - start) * 100 + p1 - p0

    @property
    def unassigned_kills(self) -> int:
        return int(self.data.get("unassigned_kills", 0))

    @property
    def party(self) -> bool:
        return int(self.data.get("party_size", 1) or 1) > 1

    @property
    def items_value(self) -> int | None:
        """Mesos worth of the items used (at the user's buy prices); None when not tracked."""
        return self.mesos_spent if self.has_items_used and "mesos_spent" in self.data else None

    def entries(self, kind: str) -> list[Entry]:
        """kind: 'kill' | 'loot' | 'used'. Falls back to name-only dicts from older files."""
        rows = self.data.get(f"{kind}_list")
        if rows is not None:
            return [Entry(r.get("id"), r.get("name", "?"), int(r.get("count", 0))) for r in rows]
        legacy = self.data.get({"kill": "kills", "loot": "loot", "used": "used"}[kind]) or {}
        return [Entry(None, name, int(n)) for name, n in sorted(legacy.items(), key=lambda kv: -kv[1])]


def load(directory: Path) -> list[SavedSession]:
    """All sessions in ``directory``, newest first. Unreadable files are skipped."""
    out = []
    for f in directory.glob("*.json") if directory.exists() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):  # not a session file
            continue
        try:
            out.append(SavedSession(f, data))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda s: s.started, reverse=True)
    return out


@dataclass
class DayTotals:
    day: str
    sessions: int = 0
    seconds: int = 0
    exp: int = 0
    kills: int = 0
    levels: int = 0
    mesos: int = 0
    mesos_spent: int = 0

    @property
    def net_mesos(self) -> int:
        return self.mesos - self.mesos_spent

    @property
    def exp_per_min(self) -> float:
        return self.exp / self.seconds * 60 if self.seconds else 0.0


def by_day(sessions: list[SavedSession]) -> list[DayTotals]:
    days: dict[str, DayTotals] = defaultdict(lambda: DayTotals(""))
    for s in sessions:
        d = days[s.day]
        d.day = s.day
        d.sessions += 1
        d.seconds += s.seconds
        d.exp += s.exp
        d.kills += s.kills
        d.levels += s.levels
        d.mesos += s.mesos
        d.mesos_spent += s.mesos_spent
    return sorted(days.values(), key=lambda d: d.day, reverse=True)
