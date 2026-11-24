"""Items used from the quickslots, what they cost, and which item sits in which slot.

* ``QuickslotUsage`` keeps, per session, a series of stack counts per assigned
  item and derives the number used from it (see the notes under "usage").
* ``SlotItems`` is the user's assignment of items to quickslots, with each
  slot's icon fingerprint at the time. MapleStory Classic's icons don't match
  the maplestory.io (MCW) ones, so we never guess: unassigned slots aren't
  tracked.
* ``PriceList`` is the user's shop *buy* prices (maplestory.io only has NPC
  sell-back prices), used for mesos spent.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

SLOT_KEYS = ("Shift", "D", "Ctrl", "T", "Del", "End", "PgDn", "Ins")  # default layout; labels only
SAME_ICON = 8.0  # mean abs BGR difference between fingerprints of the same icon


def slot_name(slot: int) -> str:
    return f"Slot {slot + 1}" + (f" ({SLOT_KEYS[slot]})" if slot < len(SLOT_KEYS) else "")


def _icon_diff(a: Sequence[float], b: Sequence[float]) -> float:
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    return float(np.abs(a - b).mean()) if a.shape == b.shape else float("inf")


# --- usage ---------------------------------------------------------------------
#
# Usage is *derived*, never accumulated: each session stores, per item
# fingerprint, a run-length-encoded series of the stack counts it saw, and the
# number used is recomputed from that series (``count_uses``). When the session
# ends the result is frozen. Nothing carries over between sessions, or across a
# pause (a resume starts a new segment), so a correction can never reach into
# another session's numbers.
#
# What makes this hard: a still stack renders the same digit pixels every
# frame and the OCR is cached by those pixels, so a misread ("13" for 18) can
# repeat for minutes until the number next changes. Overcounting is worse than
# undercounting (uses are priced into mesos spent), so:
#
# * Every booked amount is bounded by ``K`` per observed change of the reading,
#   however long the gap before it (unreadable, covered, or a different icon):
#   a step of at most ``K`` is booked if it goes down; a larger step, gap or
#   not, books nothing and opens a *suspect* segment.
# * The first reading of a segment is *provisional*: the reader's confirmed
#   value can be a stable misread carried in from before. It is trusted once a
#   plausible down-step from it is seen. A big step away from a provisional
#   anchor never books the jump; only the later plausible steps count.
# * A suspect segment is closed by a later reading, checked in this order:
#   - rebase: a plausible down-step from the latest suspect value means that
#     value was real (a restock, a big drop): book only that step. If the jump
#     into the suspect went down across a gap from a trusted anchor, and the
#     reading right after the gap is the one confirmed, also book min(jump, K)
#     for the gap (120, gap, 80, 79 -> 3 + 1);
#   - reconnect: if the reading is at most ``K`` below the anchor (or equal),
#     and one digit edit (not the identity) turns the first suspect value into
#     something in [reading, anchor], the suspect was a misread: book
#     anchor - reading (18, "13", 17 -> 1).
#   A suspect still open when the session ends is dropped.
# * Small up-steps are gains (loot) and are ignored, never netted against uses,
#   with one exception: an up-step that lands exactly on the value just before
#   the last committed change, a down-step at most ``RETURN_TICKS`` ago, cancels
#   that down-step's booking (a still 100 that reads "98" for a moment books 0).
#   A potion looted right after a use is then missed; that undercount is fine.
# * A misread first reading of a segment can still book up to ``K`` (it is
#   provisional, but a plausible step from it is believed). Accepted.
#
# Which item a reading belongs to is the user's call, never guessed: only
# quickslots assigned in the "Items & prices" dialog are tracked, and a slot's
# reading counts for its item only while the slot's icon is within
# ``SAME_ICON`` of the icon it had when assigned. Anything else (a hover
# highlight, a tooltip, another item dragged in) is a gap, never a new item.
# Two slots assigned the same item are combined per tick: their value if they
# agree; if only one is readable, its value only when it is within ``K`` of the
# item's last value; otherwise a gap. A slot that empties books nothing: we
# can't tell an empty slot from a moved stack, and undercounting is safe.

K = 3  # most uses that are plausible in one observed change (~1 s)
RETURN_TICKS = 30  # an up-step back to the value before a down-step this recent cancels it


def _near(n: int) -> set[int]:
    """Numbers one digit edit (substitute, drop or add a digit) away from ``n``, excluding ``n``."""
    s, digits = str(n), "0123456789"
    out = set()
    for i in range(len(s) + 1):
        out.update(s[:i] + d + s[i:] for d in digits)
    for i in range(len(s)):
        out.add(s[:i] + s[i + 1:])
        out.update(s[:i] + d + s[i + 1:] for d in digits)
    return {int(x) for x in out if x} - {n}


def count_uses(runs: Sequence[Sequence[int]], k: int = K) -> int:
    """Items used in one segment of ``[t_first, t_last, count]`` runs (ticks, in order)."""
    total = 0
    anchor: tuple[int, int] | None = None  # (last tick, count) of the baseline
    trusted = False  # a plausible down-step from the baseline has been seen
    sus: dict | None = None  # open suspect segment
    down: tuple[int, int, int, bool] | None = None  # last committed change, if a down-step:
    # (tick it first showed, value before it, booked, ``trusted`` before it)
    for t0, t1, v in runs:
        if anchor is None:
            anchor = (t1, v)  # provisional
            continue
        if sus is None:
            at, a = anchor
            if abs(a - v) <= k:
                if v < a:
                    total += a - v
                    down = (t0, a, a - v, trusted)
                    trusted = True
                else:  # a gain (ignored), unless it exactly undoes a recent down-step
                    if down is not None and v == down[1] and t0 - down[0] <= RETURN_TICKS:
                        total -= down[2]  # "98" for a moment on a still 100: not a use
                        trusted = down[3]
                    down = None
                anchor = (t1, v)
            else:  # implausible either way: book nothing, wait and see
                sus = {"anchor": a, "trusted": trusted, "gap": t0 - at > 1,
                       "first": v, "cur": v, "moved": False}
                down = None
            continue
        a, c = sus["anchor"], sus["cur"]
        if 0 < c - v <= k:  # rebase: c is real, confirmed by a plausible down-step
            if sus["trusted"] and sus["gap"] and not sus["moved"] and a > c:
                total += min(a - c, k)  # uses during the gap: one change's worth at most
            total += c - v
            sus, anchor, trusted = None, (t1, v), True
        elif 0 <= a - v <= k and any(v <= m <= a for m in _near(sus["first"])):
            total += a - v  # reconnect: the suspect was a misread
            trusted = sus["trusted"] or a > v
            sus, anchor = None, (t1, v)
        else:
            sus["cur"], sus["moved"] = v, True
    return total


@dataclass
class ItemSeries:
    """One item's stack counts during a session, run-length encoded, split at pauses."""

    key: str  # normalised item name: one series per assigned item
    name: str
    item_id: int | None = None
    segments: list[list[list[int]]] = field(default_factory=lambda: [[]])
    _split: bool = False
    _cache: tuple[int, int] | None = None  # (series length, uses)

    def add(self, t: int, count: int, since: int = 0) -> None:
        """``count`` read at tick ``t``; it first showed ``since`` ticks earlier."""
        if self._split and self.segments[-1]:
            self.segments.append([])
        self._split = False
        runs = self.segments[-1]
        if runs and runs[-1][2] == count:
            runs[-1][1] = max(runs[-1][1], t)
            return
        start = t - since
        if runs:
            start = max(start, runs[-1][1] + 1)
        runs.append([min(start, t), t, count])

    def new_segment(self) -> None:
        self._split = True

    def last(self) -> tuple[int, int] | None:
        """(tick, count) of the latest reading in the current segment."""
        runs = self.segments[-1]
        return (runs[-1][1], runs[-1][2]) if runs and not self._split else None

    def __len__(self) -> int:
        return sum(len(s) + 1 for s in self.segments)

    def uses(self) -> int:
        n = len(self)
        if self._cache is None or self._cache[0] != n:
            self._cache = (n, sum(count_uses(s) for s in self.segments))
        return self._cache[1]


class QuickslotUsage:
    """Per-session usage of the items the user assigned to quickslots, one series per item."""

    def __init__(self) -> None:
        self.items: dict[str, ItemSeries] = {}
        self.final: list[tuple[ItemSeries, int]] | None = None

    def record(self, t: int, reads: Sequence[tuple[int | None, Sequence[float] | None, int]],
               slots: SlotItems | None) -> None:
        """One tick of ``(count, signature, since)`` per slot (``since``: ticks the count was already showing).

        Only assigned slots count, and only while they show the assigned icon.
        """
        if self.final is not None or slots is None:
            return
        groups: dict[str, list[tuple[SlotAssignment, int | None, int]]] = {}
        for slot, a in slots.slots.items():
            count, sig, since = reads[slot] if slot < len(reads) else (None, None, 0)
            if count is None or not a.matches(sig):
                count = None  # unreadable, highlighted, covered or another item: a gap
            groups.setdefault(a.key, []).append((a, count, since))
        for key, entries in groups.items():
            item = self.items.get(key)
            if item is None:
                item = self.items[key] = ItemSeries(key, entries[0][0].name)
            item.item_id = item.item_id or next((a.item_id for a, _, _ in entries if a.item_id), None)
            got = [(c, s) for _, c, s in entries if c is not None]
            counts = {c for c, _ in got}
            if len(counts) != 1:
                continue  # nothing readable, or the item's slots disagree: a gap
            count = counts.pop()
            if len(got) < len(entries):  # only some of the item's slots are readable
                last = item.last()
                if last is None or abs(last[1] - count) > K:
                    continue  # a lone slot must agree with the item's last value
            item.add(t, count, max(s for _, s in got))

    def new_segment(self) -> None:
        """After a pause: don't diff across it."""
        for it in self.items.values():
            it.new_segment()

    def uses(self) -> list[tuple[ItemSeries, int]]:
        if self.final is not None:
            return self.final
        return [(it, n) for it in self.items.values() if (n := it.uses())]

    def finalise(self) -> None:
        if self.final is None:
            self.final = self.uses()


# --- settings files ------------------------------------------------------------


def write_atomic(path: Path, text: str) -> None:
    """Write via a temp file and ``os.replace``, so a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class _JsonFile:
    """Loads a settings file without ever losing it.

    Bad entries are skipped one by one. If the file can't be parsed, or entries
    were skipped, it is first copied to ``<name>.bad-<timestamp>.json`` so the
    next save can't wipe the user's data; ``problem`` then says what happened.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.problem: str | None = None

    def _load(self):
        if not (self.path and self.path.exists()):
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as e:
            self.problem = f"Couldn't read {self.path.name}: {e}"
        except ValueError:
            self._backup(f"{self.path.name} is damaged and couldn't be read")
        return None

    def _backup(self, what: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bad = self.path.with_name(f"{self.path.stem}.bad-{stamp}.json")
        try:
            shutil.copy2(self.path, bad)
            self.problem = f"{what}; the original was saved as {bad.name}"
        except OSError as e:
            self.problem = f"{what}, and backing it up failed ({e})"


# --- slot identity -------------------------------------------------------------


@dataclass
class SlotAssignment:
    """The user's "slot N holds this item", with the slot's icon when it was assigned."""

    slot: int
    signature: list[float]
    item_id: int | None
    name: str

    @property
    def key(self) -> str:
        return norm_name(self.name)

    def matches(self, signature: Sequence[float] | None) -> bool:
        """Whether the slot still shows the assigned icon."""
        return signature is not None and _icon_diff(signature, self.signature) <= SAME_ICON


class SlotItems(_JsonFile):
    """Quickslot -> item, as assigned by the user. Stored as JSON."""

    def __init__(self, path: Path | None = None):
        super().__init__(path)
        self.slots: dict[int, SlotAssignment] = {}
        raw = self._load()
        if raw is None:
            return
        skipped = 0
        for d in raw if isinstance(raw, list) else [raw]:
            try:
                slot, sig, name = int(d["slot"]), [float(v) for v in d["signature"]], str(d["name"])
                item_id = d.get("id")
                if slot < 0 or not norm_name(name):
                    raise ValueError(name)
                self.slots[slot] = SlotAssignment(slot, sig, None if item_id is None else int(item_id), name)
            except (KeyError, TypeError, ValueError, AttributeError):
                skipped += 1
        if skipped:
            self._backup(f"{skipped} bad entr{'y' if skipped == 1 else 'ies'} in {self.path.name} skipped")

    def get(self, slot: int) -> SlotAssignment | None:
        return self.slots.get(slot)

    def assign(self, slot: int, signature: Sequence[float] | None, item_id: int | None, name: str) -> None:
        """Assign ``name`` to ``slot`` as it shows ``signature`` now; an empty name unassigns it."""
        if name and signature is not None:
            sig = [round(float(v), 1) for v in signature]
            self.slots[slot] = SlotAssignment(slot, sig, item_id, name)
        else:
            self.slots.pop(slot, None)
        self.save()

    def save(self) -> None:
        if self.path:
            data = [{"slot": a.slot, "signature": a.signature, "id": a.item_id, "name": a.name}
                    for a in sorted(self.slots.values(), key=lambda a: a.slot)]
            write_atomic(self.path, json.dumps(data))


# --- prices --------------------------------------------------------------------


def item_key(item_id: int | None, name: str) -> str:
    """Price-list key: the item id when known, else the name."""
    return str(item_id) if item_id is not None else name


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class PriceList(_JsonFile):
    """User-entered shop buy prices: ``{"2000001": {"name": "Orange Potion", "price": 160}}``.

    Plain ``{"2000001": 160}`` entries are accepted too.
    """

    def __init__(self, path: Path | None = None):
        super().__init__(path)
        self.items: dict[str, dict] = {}
        raw = self._load()
        if raw is None:
            return
        if not isinstance(raw, dict):
            self._backup(f"{self.path.name} isn't a price list")
            return
        skipped = 0
        for k, v in raw.items():
            try:
                if isinstance(v, dict):
                    self.items[str(k)] = {"name": str(v.get("name", k)), "price": int(v.get("price", 0))}
                else:
                    self.items[str(k)] = {"name": str(k), "price": int(v)}
            except (TypeError, ValueError):
                skipped += 1
        if skipped:
            self._backup(f"{skipped} bad price{'' if skipped == 1 else 's'} in {self.path.name} skipped")

    def price(self, key: str, name: str = "") -> int | None:
        """Price by key (item id or name), else by normalised name.

        A price added while the id lookup failed is keyed by name; usage of the
        same item may be keyed by id later, and should still find it.
        """
        e = self.items.get(key)
        if e:
            return e["price"]
        n = norm_name(name or ("" if key.isdigit() else key))
        if n:
            for k, e in self.items.items():
                if norm_name(e["name"]) == n or norm_name(k) == n:
                    return e["price"]
        return None

    def set(self, key: str, name: str, price: int) -> None:
        self.items[key] = {"name": name, "price": int(price)}

    def remove(self, key: str) -> None:
        self.items.pop(key, None)

    def save(self) -> None:
        if self.path:
            write_atomic(self.path, json.dumps(self.items, indent=2))


def spent(used: dict[str, int], prices: PriceList, names: dict[str, str] | None = None) -> tuple[int, int]:
    """(mesos spent, items used that have no price) for {price key: count}."""
    total = unpriced = 0
    for key, n in used.items():
        p = prices.price(key, (names or {}).get(key, ""))
        if p is None:
            unpriced += n
        else:
            total += p * n
    return total, unpriced


def data_path(name: str) -> Path:
    from .paths import sub

    return sub("settings") / name
