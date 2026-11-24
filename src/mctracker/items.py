"""Items used from the quickslots, what they cost, and which item sits in which slot.

* ``UsageTracker`` turns per-slot stack counts (read ~1/s) into "used N" events.
  A new count must be read twice in a row before it is believed; increases
  (restocking, looting) and unreadable reads are ignored, and so are drops too
  large to be potion use (moving a stack, dropping items).
* ``SlotItems`` identifies a slot's item by its icon fingerprint. MapleStory
  Classic's icons don't match the maplestory.io (MCW) ones, so the user tells us
  once which item an icon is; that is remembered across sessions. Until then
  usage is kept per slot.
* ``PriceList`` is the user's shop *buy* prices (maplestory.io only has NPC
  sell-back prices), used for mesos spent.
"""

from __future__ import annotations

import json
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


@dataclass
class _Slot:
    count: int | None = None  # believed stack count
    pending: int | None = None  # new value seen once, awaiting confirmation
    icon: np.ndarray | None = None  # fingerprint of the item the count belongs to
    pending_icon: np.ndarray | None = None


@dataclass
class UsageTracker:
    confirm: int = 2  # consecutive identical reads needed to accept a new count
    max_drop: int = MAX_DROP
    slots: dict[int, _Slot] = field(default_factory=dict)
    _seen: dict[int, int] = field(default_factory=dict)  # slot -> times its pending value was read

    def reset(self) -> None:
        self.slots.clear()
        self._seen.clear()

    def update(self, slot: int, count: int | None, icon: Sequence[float] | None = None) -> int:
        """Feed one read of a slot; returns how many items were used (0 if none)."""
        s = self.slots.setdefault(slot, _Slot())
        if icon is not None:
            icon = np.asarray(icon, np.float32)
            if s.icon is not None and _icon_diff(icon, s.icon) > SAME_ICON:
                # Different item in the slot (or something covering it): a count
                # drop now would not be use. Start over once the new icon is stable.
                if s.pending_icon is not None and _icon_diff(icon, s.pending_icon) <= SAME_ICON:
                    self.slots[slot] = _Slot(icon=icon, pending=count)
                    self._seen[slot] = 1
                else:
                    s.pending_icon = icon
                return 0
            s.pending_icon = None
            if s.icon is None:
                s.icon = icon
        if count is None:
            return 0
        if count == s.count:
            s.pending = None
            return 0
        if count != s.pending:
            s.pending = count
            self._seen[slot] = 1
        else:
            self._seen[slot] = self._seen.get(slot, 1) + 1
        if self._seen[slot] < self.confirm:
            return 0
        old, s.count, s.pending = s.count, count, None
        if old is None or count > old or old - count > self.max_drop:
            return 0
        return old - count


# --- slot identity -------------------------------------------------------------


@dataclass
class KnownIcon:
    signature: list[float]
    item_id: int | None
    name: str


class SlotItems:
    """Icon fingerprint -> item, as assigned by the user. Stored as JSON."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.known: list[KnownIcon] = []
        if path and path.exists():
            try:
                for d in json.loads(path.read_text(encoding="utf-8")):
                    self.known.append(KnownIcon(list(d["signature"]), d.get("id"), d.get("name", "")))
            except (OSError, ValueError, KeyError, TypeError):
                self.known = []

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
            self.path.write_text(json.dumps(data), encoding="utf-8")


# --- prices --------------------------------------------------------------------


def item_key(item_id: int | None, name: str) -> str:
    """Price-list key: the item id when known, else the name."""
    return str(item_id) if item_id is not None else name


class PriceList:
    """User-entered shop buy prices: ``{"2000001": {"name": "Orange Potion", "price": 160}}``.

    Plain ``{"2000001": 160}`` entries are accepted too.
    """

    def __init__(self, path: Path | None = None):
        self.path = path
        self.items: dict[str, dict] = {}
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for k, v in raw.items():
                    if isinstance(v, dict):
                        self.items[str(k)] = {"name": str(v.get("name", k)), "price": int(v.get("price", 0))}
                    else:
                        self.items[str(k)] = {"name": str(k), "price": int(v)}
            except (OSError, ValueError, AttributeError, TypeError):
                self.items = {}

    def price(self, key: str) -> int | None:
        e = self.items.get(key)
        return e["price"] if e else None

    def set(self, key: str, name: str, price: int) -> None:
        self.items[key] = {"name": name, "price": int(price)}

    def remove(self, key: str) -> None:
        self.items.pop(key, None)

    def save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.items, indent=2), encoding="utf-8")


def spent(used: dict[str, int], prices: PriceList) -> tuple[int, int]:
    """(mesos spent, items used that have no price) for {price key: count}."""
    total = unpriced = 0
    for key, n in used.items():
        p = prices.price(key)
        if p is None:
            unpriced += n
        else:
            total += p * n
    return total, unpriced


def data_path(name: str) -> Path:
    from .paths import sub

    return sub("settings") / name
