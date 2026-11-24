"""Read saved sessions (JSON files written by ``Tracker.end_session``)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


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
    return sorted(days.values(), key=lambda d: d.day, reverse=True)
