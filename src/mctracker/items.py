"""Items used from the quickslots, what they cost, and which item sits in which slot.

* ``UsageTracker`` turns per-slot stack counts (read ~1/s) into "used N" events.
  A new count must be read twice in a row (or sit inside a steady run of small
  drops) before it is believed. Repeated misreads are corrected once the true
  value shows up (see the notes above ``UsageTracker``). Unreadable reads hold
  the last value; drops too large to be potion use (moving a stack) rebase.
* ``SlotItems`` identifies a slot's item by its icon fingerprint. MapleStory
  Classic's icons don't match the maplestory.io (MCW) ones, so the user tells us
  once which item an icon is; that is remembered across sessions. Until then
  usage is kept per slot.
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
MAX_DROP = 30  # a bigger drop between two reads isn't use (moved/dropped stack)


def slot_name(slot: int) -> str:
    return f"Slot {slot + 1}" + (f" ({SLOT_KEYS[slot]})" if slot < len(SLOT_KEYS) else "")


def _icon_diff(a: Sequence[float], b: Sequence[float]) -> float:
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    return float(np.abs(a - b).mean()) if a.shape == b.shape else float("inf")


# --- usage ---------------------------------------------------------------------
#
# Per-slot reads are noisy in two ways the debouncer has to survive:
# * A still stack renders identical digit pixels and the OCR cache is keyed by
#   those pixels, so a misread repeats every frame: "read twice" isn't proof.
#   It gets corrected when the stack next changes and the true value shows up.
# * Steady potting changes the count every read, so values never repeat.
#
# Rules for a confirmed new value (see ``_accept``):
# * a drop of more than ``max_drop`` in one step is a moved/dropped stack: rebase.
# * a drop that changes the digit count by more than ``SMALL_CROSS`` (25 -> 2) is
#   suspect: keep the old count until a later value says which one was right.
# * an increase soon after a drop that lands at or below the pre-drop count means
#   the drop was a misread: book a correction so the net is the true decrease.
# * any other increase (restock, loot, or a misread like 83 -> 88) is tentative:
#   if the next drop lands at or below the pre-increase count, book from there.

LAST_FEW = 3  # a slot that goes empty from at most this many: they were used
SMALL_CROSS = 5  # a drop crossing a digit boundary by more than this is suspect
RECENT = 60  # reads (~1 min) a drop or increase stays open to correction
HOLD = 5  # reads a different icon must last before the slot's old baseline is dropped


def _digits(n: int) -> int:
    return len(str(n))


@dataclass
class _Slot:
    count: int | None = None  # believed stack count
    pending: int | None = None  # new value seen, awaiting confirmation
    seen: int = 0  # times ``pending`` was read
    icon: np.ndarray | None = None  # fingerprint of the item the count belongs to
    reads: int = 0  # reads fed to this slot: the clock for RECENT
    last_drop: tuple[int, int, int] | None = None  # (count before, uses booked, at read)
    floor: tuple[int, int] | None = None  # (count before an increase, at read)
    suspect: int | None = None  # value rejected as a likely digit-dropping misread


@dataclass
class UsageTracker:
    confirm: int = 2  # consecutive identical reads needed to accept a new count
    max_drop: int = MAX_DROP  # per step
    hold: int = HOLD
    slots: dict[int, _Slot] = field(default_factory=dict)
    _alt: dict[int, _Slot] = field(default_factory=dict)  # slot -> state under a different icon
    _away: dict[int, int] = field(default_factory=dict)  # slot -> reads the other icon has lasted
    _had: dict[int, set[int]] = field(default_factory=dict)  # slot -> other slots already showing its icon

    def reset(self) -> None:
        self.slots.clear()
        self._alt.clear()
        self._away.clear()
        self._had.clear()

    def update(self, slot: int, count: int | None, icon: Sequence[float] | None = None) -> int:
        """Feed one read of a slot; returns items used (negative = correction of earlier uses)."""
        return sum(n for _, n in self._update(slot, count, icon, None))

    def update_all(self, reads: Sequence[tuple[int | None, Sequence[float] | None]]
                   ) -> list[tuple[int, np.ndarray | None, int]]:
        """Feed one frame of (count, icon) per slot.

        Returns (slot, icon the uses belong to, n) for each non-zero change; n < 0
        corrects uses booked earlier for that icon.
        """
        icons = [None if ic is None else np.asarray(ic, np.float32) for _, ic in reads]
        out = []
        for slot, (count, _) in enumerate(reads):
            for icon, n in self._update(slot, count, icons[slot], icons):
                if n:
                    out.append((slot, icon, n))
        return out

    # --- internals ---------------------------------------------------------------

    def _update(self, slot, count, icon, icons) -> list[tuple[np.ndarray | None, int]]:
        s = self.slots.setdefault(slot, _Slot())
        if icon is not None:
            icon = np.asarray(icon, np.float32)
            if s.icon is None:
                s.icon = icon
            elif _icon_diff(icon, s.icon) > SAME_ICON:
                return self._other_icon(slot, s, count, icon, icons)
            if slot in self._alt:  # our icon is back (a tooltip moved off): carry on
                del self._alt[slot]
                self._away.pop(slot, None)
                self._had.pop(slot, None)
        return [(s.icon, self._feed(s, count))]

    def _other_icon(self, slot, s, count, icon, icons) -> list[tuple[np.ndarray | None, int]]:
        """A different icon in the slot: another item, an empty slot, or something covering it.

        Its reads are tracked on their own; the old baseline is kept for ``hold``
        reads in case the old icon comes back (a tooltip moving off).
        """
        a = self._alt.get(slot)
        if a is None or _icon_diff(icon, a.icon) > SAME_ICON:
            a = self._alt[slot] = _Slot(icon=icon)
            self._away[slot] = 0
            if slot not in self._had:  # slots that already held this item (by their settled icon)
                self._had[slot] = {j for j, o in self.slots.items()
                                   if j != slot and o.icon is not None and _icon_diff(o.icon, s.icon) <= SAME_ICON}
        self._away[slot] += 1
        out = [(icon, self._feed(a, count))]
        if self._away[slot] >= self.hold:
            had = self._had.pop(slot, set())
            self.slots[slot] = self._alt.pop(slot)
            del self._away[slot]
            # The slot went empty: the last few were used, unless the stack moved.
            if a.count is None and a.pending is None and s.count is not None and 0 < s.count <= LAST_FEW:
                moved = icons is not None and any(
                    j != slot and j not in had and ic is not None and _icon_diff(ic, s.icon) <= SAME_ICON
                    for j, ic in enumerate(icons))
                if not moved:
                    out.append((s.icon, s.count))
        return out

    def _feed(self, s: _Slot, count: int | None) -> int:
        s.reads += 1
        if count is None:  # unreadable or obscured: hold the last value
            return 0
        if count == s.count:
            s.pending, s.seen = None, 0
            return 0
        booked = 0
        if count == s.pending:
            s.seen += 1
        else:
            # Steady potting: a read strictly between the count and this read
            # (each step a plausible use) is confirmed by its neighbours.
            p, c = s.pending, s.count
            if p is not None and p > count and p - count <= self.max_drop and (
                    c is None or (c > p and c - p <= self.max_drop)):
                booked = self._accept(s, p)
            s.pending, s.seen = count, 1
        if s.seen >= self.confirm:
            s.pending, s.seen = None, 0
            booked += self._accept(s, count)
        return booked

    def _accept(self, s: _Slot, value: int) -> int:
        """A confirmed new value; returns uses to book (negative = correction)."""
        old = s.count
        if old is None:
            s.count = value
            return 0

        def recent(at: int) -> bool:
            return s.reads - at <= RECENT

        if value > old:
            if s.last_drop and recent(s.last_drop[2]) and value <= s.last_drop[0]:
                # The drop was a misread (18 -> "13" -> 17): net it to the true decrease.
                pre, booked, _ = s.last_drop
                s.count, s.last_drop, s.floor = value, None, None
                return (pre - value) - booked
            s.count, s.floor, s.suspect, s.last_drop = value, (old, s.reads), None, None
            return 0
        drop = old - value
        if drop > self.max_drop:  # moved or dropped a stack
            s.count, s.last_drop, s.floor, s.suspect = value, None, None, None
            return 0
        if _digits(value) != _digits(old) and drop > SMALL_CROSS:
            # 25 read as "2": believe it only once a later value follows on from it.
            if not (s.suspect is not None and value < s.suspect and _digits(value) == _digits(s.suspect)):
                s.suspect = value
                return 0
        base = old
        if s.floor and recent(s.floor[1]) and value <= s.floor[0]:
            base = s.floor[0]  # the increase was a misread (83 -> "88" -> 82)
        s.count, s.floor, s.suspect = value, None, None
        n = base - value
        s.last_drop = (base, n, s.reads) if n > 0 else None
        return max(0, n)


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
