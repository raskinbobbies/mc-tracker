"""Items used from the quickslots, what they cost, and which item sits in which slot.

* ``QuickslotUsage`` keeps, per session, a series of stack counts per assigned
  item and slot, and derives the number used from its net change (see the
  notes under "usage").
* ``SlotItems`` is the user's assignment of items to quickslots, with each
  slot's icon fingerprint at the time. MapleStory Classic's icons don't match
  the maplestory.io (MCW) ones, so we never guess: unassigned slots aren't
  tracked.
* ``PriceList`` is the user's shop *buy* prices (maplestory.io only has NPC
  sell-back prices), used for mesos spent.
* ``AppSettings`` holds the app's switches. Items used is always on; an old
  ``experimental_items_used`` key in the file is ignored.

Known issues (items used / mesos spent can be off until these are fixed):

* Overcount: two consecutive values misread the same way (e.g. a cursor over
  the count while potting) pass the stability check and can book about +60
  per event.
* Undercount: steady potting at 1 per tick after a hover can book about 0,
  since no value holds long enough and the descending chain is broken.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
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
# Usage is *net change*, never booked step by step. Each session stores, per
# assigned item and per quickslot showing it, a run-length-encoded series of
# the stack counts read, and the number used is recomputed from it:
#
# 1. Stable values. A value is stable once it has held for at least ``S``
#    readable ticks (gaps neither count nor break it), or when it is part of a
#    descending chain of at least ``S`` consecutive readings, each 1..``K``
#    below the last (steady potting never holds a value). Brief misreads
#    never become stable, so they can't matter at all.
# 2. Segments. The stable values are split at restocks: an up-jump between
#    stable values of more than ``R``. Smaller ups are loot: they reduce the
#    net and are never counted as uses. Before splitting, a dip (or spike)
#    that comes straight back is dropped as a stable misread: one of more
#    than ``R`` that comes back to within ``R`` ("10" for a still 100 isn't
#    a restock), or an unlinked one (rule 4) of more than ``K`` that comes
#    back to within ``K`` ("13" for 18, then 17).
# 3. Uses per segment = max(0, first stable value - last stable value).
#    Everything in between (misreads, hovers, flicker, loot) cancels by
#    construction, so a misread can no longer cost anything per occurrence.
#    While the session runs, the latest stable value is the provisional end.
# 4. Endpoint sanity. An endpoint that would add more than ``K`` uses over
#    its in-segment neighbour is suspect, and replaced by that neighbour
#    (repeatedly, for two misreads in a row), unless readings each within ``K`` of the last, with no gap, lead from
#    one to the other. That covers every one-digit misread that differs by
#    more than ``K`` ("12" for 120), any misread after a gap, and any step
#    faster than ``K`` per tick. A segment whose net is still faster than
#    ``K`` per tick on average falls back on its endpoints' neighbours and is
#    then clamped to ``K`` per tick.
# 5. Two slots for one item. Each slot has its own stable values. When both
#    show a stable value and disagree, the one held longer wins (ties keep
#    the item's value). A slot reading on its own (the other is covered or
#    between stable values) can't move the item further from the other
#    slot's last stable value than ``K`` per tick since that was last read. A slot that is no longer assigned to the
#    item stops counting from then on (a moved stack).
#
# Worst case, per segment: each endpoint can be a stable misread that slips
# past the check, i.e. one within ``K`` of its neighbour, or one reached by a
# gap-free chain of readings each within ``K`` of the last (which a sticky
# misread of a still stack can't produce: it is one wrong value, not a
# chain). So one misread per endpoint, at most ``K`` each, ``2*K`` per
# segment, however long the session and however many misreads in between. Undercounts are accepted: uses that never reach a stable value
# (fewer than ``S`` readable ticks), loot (it nets), real drops at an endpoint
# that look like a misread, and a small restock (within ``R``) that looks like
# a dip. A slot that empties books nothing for its last items: we can't tell
# an empty slot from a moved stack.
#
# Which item a reading belongs to is the user's call, never guessed: only
# quickslots assigned in the "Items & prices" dialog are tracked, and a
# slot's reading counts for its item only while the slot's icon is within
# ``SAME_ICON`` of the icon it had when assigned. Anything else (a hover
# highlight, a tooltip, another item dragged in) is a gap, never a new item.
# Nothing carries over between sessions, or across a pause (a resume starts a
# new segment), and the result is frozen when the session ends.

K = 3  # most uses that are plausible per tick (~1 s)
S = 5  # readable ticks a value must hold to be stable
R = 10  # an up-jump between stable values of more than this is a restock


@dataclass
class Stable:
    """A stable value: shown from tick ``t0`` to ``t1`` for ``n`` readable ticks."""

    v: int
    t0: int
    t1: int
    n: int
    prev: int | None = None  # the stable value before it in the same slot
    linked: bool = False  # reached from ``prev`` by readings each within K of the last, with no gap


def stable_values(runs: Sequence[Sequence[int]], s: int = S, k: int = K) -> list[Stable]:
    """Stable values of one slot's ``[t_first, t_last, count, reads]`` runs, equal neighbours merged."""
    ok = [r[3] >= s for r in runs]
    i = 0
    while i < len(runs):  # descending chains: steady potting
        j = i
        while j + 1 < len(runs) and 0 < runs[j][2] - runs[j + 1][2] <= k:
            j += 1
        if j - i + 1 >= s:
            ok[i:j + 1] = [True] * (j - i + 1)
        i = j + 1
    out: list[Stable] = []
    linked = False  # every raw step since the last stable value was plausible and gap-free
    for i, (r, good) in enumerate(zip(runs, ok)):
        if i:
            p = runs[i - 1]
            linked = linked and abs(p[2] - r[2]) <= k and r[0] - p[1] <= 1
        if not good:
            continue
        if out and out[-1].v == r[2]:  # a brief misread in between doesn't split a value
            out[-1].t1, out[-1].n = r[1], out[-1].n + r[3]
        else:
            prev = out[-1].v if out else None
            out.append(Stable(r[2], r[0], r[1], r[3], prev, linked and prev is not None))
        linked = True
    return out


def combine(lanes: Sequence[tuple[list[Stable], int | None]], k: int = K) -> list[Stable]:
    """One item's stable values from several slots' (stable values, tick it was unassigned or None)."""
    lanes = [(st, ret) for st, ret in lanes if st]
    if len(lanes) <= 1:
        return list(lanes[0][0]) if lanes else []
    ticks = sorted({x.t0 for st, _ in lanes for x in st} | {x.t1 + 1 for st, _ in lanes for x in st}
                   | {ret for _, ret in lanes if ret is not None})
    out: list[Stable] = []
    idx = [0] * len(lanes)
    for t in ticks:
        shown: list[Stable] = []
        last: list[Stable] = []  # last stable value of each assigned slot not showing one now
        for i, (st, ret) in enumerate(lanes):
            while idx[i] + 1 < len(st) and st[idx[i] + 1].t0 <= t:
                idx[i] += 1
            x = st[idx[i]]
            if x.t0 > t or (ret is not None and t >= ret):
                continue
            (shown if t <= x.t1 else last).append(x)
        if not shown:
            continue
        cur = out[-1].v if out else None
        best = max(x.n for x in shown)  # slots disagree: the value held longest wins
        cands = {x.v for x in shown if x.n == best}
        v = min(cands, key=lambda c: (abs(c - cur), c) if cur is not None else (0, c))  # ties keep the value
        if any(abs(v - o.v) > k * (t - o.t1) for o in last):
            continue  # a lone slot can't move the item further than play since the other slot's last value
        x = max((x for x in shown if x.v == v), key=lambda x: x.t1)
        if v == cur:
            out[-1].t1 = max(out[-1].t1, x.t1)
            continue
        if out:
            out[-1].t1 = min(out[-1].t1, t - 1)
        out.append(Stable(v, t, x.t1, best, cur, x.linked and x.prev == cur))
    return out


def _drop_dips(seq: list[Stable], k: int = K, r: int = R) -> list[Stable]:
    """Drop stable values that jump away and come straight back: misreads, not uses or restocks.

    Either a jump of more than ``r`` that comes back to within ``r`` ("10"
    for a still 100), or an unlinked jump of more than ``k`` that comes back
    to within ``k`` ("13" for 18, then 17).
    """
    seq = list(seq)
    i = 1
    while i < len(seq) - 1:
        a, b, c = seq[i - 1].v, seq[i].v, seq[i + 1].v
        if (((c - b > r or b - a > r) and abs(c - a) <= r)
                or (abs(b - a) > k and not seq[i].linked and abs(c - a) <= k)):
            del seq[i]
            seq[i].linked = False  # what follows no longer connects to its neighbour
            if seq[i - 1].v == seq[i].v:
                seq[i - 1].t1 = seq[i].t1
                del seq[i]
            i = max(1, i - 1)
        else:
            i += 1
    return seq


def _suspect(gain: int, later: Stable, k: int) -> bool:
    """Whether an endpoint worth ``gain`` more uses than its neighbour is likely a misread.

    ``later`` is whichever of the two comes second. A step of more than ``k``
    is trusted only when readings each within ``k`` of the last, with no gap,
    lead from one to the other (steady potting): a one-digit misread ("12"
    for 120), a misread after a tooltip and a big real drop across a gap all
    fail that, and fall back on the neighbour.
    """
    return gain > k and not later.linked


def net_uses(seq: Sequence[Stable], k: int = K, r: int = R) -> int:
    """Uses from one item's stable values: per restock segment, first minus last (rules 2-4)."""
    seq = _drop_dips([replace(x) for x in seq], k, r)
    segs: list[list[Stable]] = []
    for x in seq:
        if not segs or x.v - segs[-1][-1].v > r:
            segs.append([])
        segs[-1].append(x)
    total = 0
    for i, seg in enumerate(segs):
        if len(seg) < 2:
            continue
        a, b = 0, len(seg) - 1
        while a < b and _suspect(seg[a].v - seg[a + 1].v, seg[a + 1], k):
            a += 1  # a suspect first value: fall back on the next one
        while a < b and _suspect(seg[b - 1].v - seg[b].v, seg[b], k):
            b -= 1  # a suspect last value: fall back on the one before
        if a >= b:
            continue
        first, last = seg[a], seg[b]
        n = first.v - last.v
        dur = max(1, last.t0 - first.t1)
        if n > k * dur:  # faster than K per tick on average: fall back, then clamp
            n = min(n, seg[a + 1].v - last.v, first.v - seg[b - 1].v, k * dur)
        total += max(0, n)
    return total


@dataclass
class _Segment:
    lanes: dict[int, list[list[int]]] = field(default_factory=dict)  # lane -> runs
    retired: dict[int, int] = field(default_factory=dict)  # lane -> tick its slot was unassigned
    done: int | None = None  # uses, cached once a later segment exists (a finished segment never changes)

    def __len__(self) -> int:
        return sum(len(r) for r in self.lanes.values())


@dataclass
class ItemSeries:
    """One item's stack counts during a session, per slot (lane), run-length encoded, split at pauses."""

    key: str  # normalised item name: one series per assigned item
    name: str
    item_id: int | None = None
    segments: list[_Segment] = field(default_factory=lambda: [_Segment()])
    _split: bool = False
    _lane: dict[int, int] = field(default_factory=dict)  # slot -> its current lane
    _lanes: int = 0
    _ver: int = 0
    _cache: tuple[int, int] | None = None  # (version, uses)

    def _seg(self) -> _Segment:
        if self._split and len(self.segments[-1]):
            self.segments.append(_Segment())
            self._lane.clear()
            self._ver += 1
        self._split = False
        return self.segments[-1]

    def add(self, t: int, count: int, since: int = 0, slot: int = 0) -> None:
        """``count`` read in ``slot`` at tick ``t``; it was already showing for ``since`` reads."""
        seg = self._seg()
        if slot not in self._lane:
            self._lane[slot] = self._lanes
            self._lanes += 1
        runs = seg.lanes.setdefault(self._lane[slot], [])
        if runs and runs[-1][2] == count:
            runs[-1][1] = max(runs[-1][1], t)
            runs[-1][3] += 1
            # Extending a run only matters when it becomes stable, or when two slots
            # compare how long each value has held (their ``n``).
            if runs[-1][3] == S or len(seg.lanes) > 1:
                self._ver += 1
            return
        self._ver += 1  # a new run
        start = t - since
        if runs:
            start = max(start, runs[-1][1] + 1)
        runs.append([min(start, t), t, count, 1 + max(0, since)])

    def retire(self, slot: int, t: int) -> None:
        """``slot`` is no longer assigned to this item: its readings stop counting from ``t``."""
        lane = self._lane.pop(slot, None)
        if lane is not None and lane in self.segments[-1].lanes:
            self.segments[-1].retired[lane] = t
            self._ver += 1

    def slots(self) -> set[int]:
        return set(self._lane)

    def new_segment(self) -> None:
        self._split = True

    def __len__(self) -> int:
        return sum(len(s) + 1 for s in self.segments)

    def uses(self) -> int:
        if self._cache is None or self._cache[0] != self._ver:
            n = 0
            for i, seg in enumerate(self.segments):
                if seg.done is None:
                    u = net_uses(combine([(stable_values(runs), seg.retired.get(lane))
                                          for lane, runs in seg.lanes.items()]))
                    if i == len(self.segments) - 1:
                        n += u  # the last segment can still grow
                        continue
                    seg.done = u
                n += seg.done
            self._cache = (self._ver, n)
        return self._cache[1]


class QuickslotUsage:
    """Per-session usage of the items the user assigned to quickslots, one series per item."""

    def __init__(self) -> None:
        self.items: dict[str, ItemSeries] = {}
        self.final: list[tuple[ItemSeries, int]] | None = None

    def record(self, t: int, reads: Sequence[tuple[int | None, Sequence[float] | None, int]],
               slots: SlotItems | None) -> None:
        """One tick of ``(count, signature, since)`` per slot (``since``: reads the count was already showing).

        Only assigned slots count, and only while they show the assigned icon.
        """
        if self.final is not None or slots is None:
            return
        assigned: dict[str, set[int]] = {}
        for slot, a in slots.slots.items():
            assigned.setdefault(a.key, set()).add(slot)
            item = self.items.get(a.key)
            if item is None:
                item = self.items[a.key] = ItemSeries(a.key, a.name)
            item.item_id = item.item_id or a.item_id
            count, sig, since = reads[slot] if slot < len(reads) else (None, None, 0)
            if count is not None and a.matches(sig):  # else unreadable, highlighted, covered or another item
                item.add(t, count, since, slot)
        for key, item in self.items.items():
            for slot in item.slots() - assigned.get(key, set()):
                item.retire(slot, t)  # moved or reassigned: stop counting that slot

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


# --- app settings --------------------------------------------------------------


OVERLAY_SCALE_MIN, OVERLAY_SCALE_MAX = 0.6, 2.0


class AppSettings(_JsonFile):
    """The app's switches, stored as JSON next to the other settings files.

    ``overlay_scale``: size of the overlay, 0.6 to 2.0 (1.0 = 100%).
    ``windows``: where each window was last (``"main"``, ``"overlay"``); see ``windowstate``.
    Unusable window entries are dropped quietly: a lost position isn't worth a backup file.
    """

    DEFAULTS = {"overlay_scale": 1.0}

    def __init__(self, path: Path | None = None):
        super().__init__(path)
        self.overlay_scale = 1.0
        self.windows: dict[str, dict] = {}
        raw = self._load()
        if raw is None:
            return
        if not isinstance(raw, dict):
            self._backup(f"{self.path.name} isn't a settings file")
            return
        bad = []
        s = raw.get("overlay_scale", 1.0)
        if isinstance(s, (int, float)) and not isinstance(s, bool) and OVERLAY_SCALE_MIN <= s <= OVERLAY_SCALE_MAX:
            self.overlay_scale = float(s)
        else:
            bad.append("overlay_scale")
        from .windowstate import clean_saved

        wins = raw.get("windows")
        if isinstance(wins, dict):
            for key, d in wins.items():
                if (c := clean_saved(d)) is not None:
                    self.windows[str(key)] = c
        if bad:
            self._backup(f"Bad {', '.join(bad)} in {self.path.name} (using the default)")

    def save(self) -> None:
        if self.path:
            data: dict = {"overlay_scale": self.overlay_scale}
            if self.windows:
                data["windows"] = self.windows
            write_atomic(self.path, json.dumps(data, indent=2))


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
        skipped = old = 0
        for d in raw if isinstance(raw, list) else [raw]:
            if isinstance(d, dict) and "signature" in d and "slot" not in d:
                old += 1  # the earlier per-icon format: there is no slot to restore it to
                continue
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
        elif old:  # a format update, not damage: save it once, so it isn't reported again
            try:
                self.save()
                self.problem = (f"Quickslot assignments now belong to slots: assign your items again "
                                f"({old} from the old format {'was' if old == 1 else 'were'} cleared)")
            except OSError as e:
                self.problem = f"Couldn't update {self.path.name} to the new format: {e}"

    def get(self, slot: int) -> SlotAssignment | None:
        return self.slots.get(slot)

    def assign(self, slot: int, signature: Sequence[float] | None, item_id: int | None, name: str,
               shown: Sequence[Sequence[float] | None] | None = None) -> None:
        """Assign ``name`` to ``slot`` as it shows ``signature`` now; an empty name unassigns it.

        ``shown`` is every slot's current icon: the item's other assignments
        whose slot doesn't show it any more are dropped (the stack was moved).
        """
        if name and signature is not None:
            sig = [round(float(v), 1) for v in signature]
            a = self.slots[slot] = SlotAssignment(slot, sig, item_id, name)
            if shown is not None:
                for s, b in list(self.slots.items()):
                    now = shown[s] if s < len(shown) else None
                    if s != slot and b.key == a.key and not (b.matches(now) or a.matches(now)):
                        del self.slots[s]
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
