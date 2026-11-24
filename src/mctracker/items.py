"""Items used from the quickslots, what they cost, and which item sits in which slot.

* ``QuickslotUsage`` keeps, per session, a series of stack counts per item
  icon and derives the number used from it (see the notes under "usage").
* ``SlotItems`` identifies a slot's item by its icon fingerprint. MapleStory
  Classic's icons don't match the maplestory.io (MCW) ones, so the user tells us
  once which item an icon is; that is remembered across sessions. Until then
  usage is labelled with the slot the icon was last seen in.
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
# * Only plausible down-steps are booked: at most ``K`` per tick, scaled by the
#   ticks elapsed across gaps (unreadable, covered, or a different icon).
# * Any other step opens a *suspect* segment and nothing is booked for it. A
#   later reading closes it:
#   - if it lands at or below the value before the suspect (the anchor), and
#     the suspect value is one digit away from something in [reading, anchor],
#     the suspect was a misread: book anchor - reading, if that is plausible for
#     the ticks since the anchor (18, "13", 17 -> 1);
#   - else a plausible down-step from the suspect value means the jump was real
#     (a restock, a big drop, a sale): rebase there and book only that step.
#   A suspect still open when the session ends is dropped.
# * Small up-steps are gains (loot) and are ignored, never netted against uses.
# * Two slots holding the same item show the same total; if they disagree, that
#   tick is a gap. A slot that empties books nothing: we can't tell an empty
#   slot from a moved stack reliably, and undercounting is the safe side.

K = 3  # most uses that are plausible in one tick (~1 s)


def _near(n: int) -> set[int]:
    """Numbers one digit edit (substitute, drop or add a digit) away from ``n``, and ``n``."""
    s, digits = str(n), "0123456789"
    out = {s}
    for i in range(len(s) + 1):
        out.update(s[:i] + d + s[i:] for d in digits)
    for i in range(len(s)):
        out.add(s[:i] + s[i + 1:])
        out.update(s[:i] + d + s[i + 1:] for d in digits)
    return {int(x) for x in out if x}


def count_uses(runs: Sequence[Sequence[int]], k: int = K) -> int:
    """Items used in one segment of ``[t_first, t_last, count]`` runs (ticks, in order)."""
    total = 0
    prev: tuple[int, int] | None = None  # (last tick, count) of the trusted baseline
    sus: dict | None = None  # open suspect segment
    for t0, t1, v in runs:
        if prev is None:
            prev = (t1, v)
            continue
        if sus is None:
            pt, pv = prev
            if abs(pv - v) <= k * (t0 - pt):
                total += max(0, pv - v)  # a use, a gain (ignored) or no change
                prev = (t1, v)
            else:  # implausible either way: book nothing, wait and see
                sus = {"anchor": pv, "anchor_t": pt, "value": v, "cur": v, "cur_t": t1}
            continue
        a = sus["anchor"]
        if v <= a and 0 <= a - v <= k * (t0 - sus["anchor_t"]) and any(v <= m <= a for m in _near(sus["value"])):
            total += a - v  # the suspect was a misread: net difference
            sus, prev = None, (t1, v)
        elif 0 < sus["cur"] - v <= k * (t0 - sus["cur_t"]):
            total += sus["cur"] - v  # the jump was real: rebase, book this step
            sus, prev = None, (t1, v)
        else:
            sus["cur"], sus["cur_t"] = v, t1
    return total


@dataclass
class ItemSeries:
    """One item's stack counts during a session, run-length encoded, split at pauses."""

    signature: np.ndarray
    slot: int  # slot it was last seen in, to label it until the user names it
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

    def __len__(self) -> int:
        return sum(len(s) + 1 for s in self.segments)

    def uses(self) -> int:
        n = len(self)
        if self._cache is None or self._cache[0] != n:
            self._cache = (n, sum(count_uses(s) for s in self.segments))
        return self._cache[1]


class QuickslotUsage:
    """Per-session usage of the items in the quickslots, keyed by icon fingerprint."""

    def __init__(self) -> None:
        self.items: list[ItemSeries] = []
        self.final: list[tuple[ItemSeries, int]] | None = None

    def _item(self, sig: np.ndarray, slot: int) -> ItemSeries:
        best = min(self.items, key=lambda it: _icon_diff(sig, it.signature), default=None)
        if best is not None and _icon_diff(sig, best.signature) <= SAME_ICON:
            return best
        self.items.append(ItemSeries(sig, slot))
        return self.items[-1]

    def record(self, t: int, reads: Sequence[tuple[int | None, Sequence[float] | None, int]]) -> None:
        """One tick of ``(count, signature, since)`` per slot (``since``: ticks the count was already showing)."""
        if self.final is not None:
            return
        seen: dict[int, tuple[ItemSeries, set[int], int, int]] = {}
        for slot, (count, sig, since) in enumerate(reads):
            if count is None or sig is None:
                continue  # a gap, never an event
            item = self._item(np.asarray(sig, np.float32), slot)
            _, values, s, first = seen.get(id(item), (item, set(), 0, slot))
            values.add(count)
            seen[id(item)] = (item, values, max(s, since), first)
        for item, values, since, slot in seen.values():
            if len(values) == 1:  # slots with the same item disagree: a gap
                item.slot = slot
                item.add(t, values.pop(), since)

    def new_segment(self) -> None:
        """After a pause: don't diff across it."""
        for it in self.items:
            it.new_segment()

    def uses(self) -> list[tuple[ItemSeries, int]]:
        if self.final is not None:
            return self.final
        return [(it, n) for it in self.items if (n := it.uses())]

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
class KnownIcon:
    signature: list[float]
    item_id: int | None
    name: str


class SlotItems(_JsonFile):
    """Icon fingerprint -> item, as assigned by the user. Stored as JSON."""

    def __init__(self, path: Path | None = None):
        super().__init__(path)
        self.known: list[KnownIcon] = []
        raw = self._load()
        if raw is None:
            return
        skipped = 0
        for d in raw if isinstance(raw, list) else [raw]:
            try:
                sig = [float(v) for v in d["signature"]]
                item_id = d.get("id")
                self.known.append(KnownIcon(sig, None if item_id is None else int(item_id), str(d.get("name", ""))))
            except (KeyError, TypeError, ValueError, AttributeError):
                skipped += 1
        if skipped:
            self._backup(f"{skipped} bad entr{'y' if skipped == 1 else 'ies'} in {self.path.name} skipped")

    def identify(self, signature: Sequence[float]) -> KnownIcon | None:
        best = min(self.known, key=lambda k: _icon_diff(signature, k.signature), default=None)
        if best is not None and _icon_diff(signature, best.signature) <= SAME_ICON:
            return best
        return None

    def assign(self, signature: Sequence[float], item_id: int | None, name: str) -> None:
        sig = [round(float(v), 1) for v in signature]
        self.known = [k for k in self.known if _icon_diff(sig, k.signature) > SAME_ICON]
        if name:
            self.known.append(KnownIcon(sig, item_id, name))
        self.save()

    def save(self) -> None:
        if self.path:
            data = [{"signature": k.signature, "id": k.item_id, "name": k.name} for k in self.known]
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
