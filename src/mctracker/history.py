"""Read saved sessions (JSON files written by ``Tracker.end_session``)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz


def total_used(*counts) -> int:
    """Items used: the sum over every count iterable (item-id and name-only alike).

    The one rule behind both the live overlay figure and the history POTIONS column.
    """
    return sum(sum(c) for c in counts)


def best_index(values: list[int]) -> int | None:
    """Index of the first highest minute, or None when there is nothing to mark."""
    if not values or max(values) <= 0:
        return None
    return values.index(max(values))


@dataclass
class Entry:
    id: int | None
    name: str
    count: int


MIN_TAIL = 8  # fewest trailing characters that may identify an item ("Banana" alone would match any banana)
MIN_NAME = 5  # shortest name (letters and digits) worth repairing
TAIL_SCORE = 85  # similarity (0-100) the shared tails need
MAX_EDITS = 2  # misread letters allowed in a long name (one in a short one)
LONG_NAME = 10  # letters and digits
ELLIPSIS = ("...", "…")


def _key(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _unique(scored: dict[int, float], best=max) -> int | None:
    """The one id with the best score, or None when nothing scored or two items tie."""
    if not scored:
        return None
    top = best(scored.values())
    winners = [i for i, v in scored.items() if v == top]
    return winners[0] if len(winners) == 1 else None


def match_known_name(name: str, known: dict[int, str]) -> int | None:
    """The id of the one known item a damaged loot name stands for, else None (never guesses between two).

    OCR damage comes in three kinds, tried in this order:
    - a misread letter or two ("Silyer Ore", "lilk Jug"): at most ``MAX_EDITS`` edits away from the whole name;
    - a name cut off with "..." ("Arrows for Cros..."): the stem is the start of exactly one item;
    - a name with another player's name glued on, or its first letters cut off ("xiaobanmiapupin's Banana",
      "oin's Banana"): the last characters match the item's last characters.
    """
    from rapidfuzz.distance import Levenshtein

    raw = name.strip()
    key = _key(raw)
    if len(key) < MIN_NAME:
        return None
    others = {i: _key(n) for i, n in known.items()}
    if raw.endswith(ELLIPSIS):
        stem = _key(raw.rstrip(".… "))
        hits = [i for i, o in others.items() if len(stem) >= MIN_TAIL and o.startswith(stem)]
        return hits[0] if len(hits) == 1 else None
    # Two edits only on a long name: "Pie Flour" and "Rye Flour" are two edits apart and are different items.
    allowed = MAX_EDITS if len(key) >= LONG_NAME else 1
    near = {i: d for i, o in others.items() if len(o) >= MIN_NAME and (d := Levenshtein.distance(key, o)) <= allowed}
    if near:  # two equally close items: ambiguous, so no guess (and no falling back to the tail rule)
        return _unique(near, best=min)
    tails = {}
    for i, o in others.items():
        n = min(len(key), len(o))
        if n >= MIN_TAIL and fuzz.ratio(key[-n:], o[-n:]) >= TAIL_SCORE:
            tails[i] = n  # the longest shared tail wins: "...pin's Banana" is Lupin's Banana, not Banana
    return _unique(tails)


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
        mins = self.exp_per_minute or []
        i = best_index(mins)
        return None if i is None else (i, mins[i])

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

    def _count(self, key: str, default: int = 0) -> int:
        """An integer field; a missing, null or garbled value reads as ``default``."""
        try:
            return int(self.data.get(key) or default)
        except (TypeError, ValueError):
            return default

    @property
    def unassigned_kills(self) -> int:
        return self._count("unassigned_kills")

    @property
    def party(self) -> bool:
        return self._count("party_size", 1) > 1

    @property
    def unpriced_used(self) -> int:
        """Kinds of item used that have no price set (their cost is missing from the mesos spent)."""
        return self._count("used_unpriced")

    @property
    def items_value(self) -> int | None:
        """Mesos worth of the items used (at the user's buy prices); None when not tracked."""
        return self.mesos_spent if self.has_items_used and "mesos_spent" in self.data else None

    def loot_view(self) -> tuple[list[Entry], list[Entry]]:
        """(loot to show, loot lines that name no known item).

        Lines with no item id are OCR leftovers: a clipped name or another player's name glued on. One that ends
        like an item of this session folds into it (the counts add); the rest are set aside, never shown as tiles.
        A file where nothing has an id (saved offline, or name-only) keeps its names: there is nothing to judge by.
        """
        rows = self.entries("loot")
        known = {e.id: e.name for e in rows if e.id is not None}
        if not known:
            return [e for e in rows if e.count > 0], []
        shown: dict[int, Entry] = {}
        unknown: list[Entry] = []
        for e in rows:
            target = e.id if e.id is not None else match_known_name(e.name, known)
            if target is None:
                unknown.append(e)
            elif target in shown:
                shown[target].count += e.count
            else:
                shown[target] = Entry(target, known[target], e.count)
        return sorted((e for e in shown.values() if e.count > 0), key=lambda e: -e.count), [e for e in unknown if e.count > 0]

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
