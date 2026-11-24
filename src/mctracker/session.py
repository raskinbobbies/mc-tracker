"""Grinding sessions: one per map visit, started by the first EXP gain.

``Tracker`` owns the lifecycle:

* waiting  - no session; on a map with monsters, the first EXP gain starts one
* active   - tracking; time only counts while you're gaining EXP (idle gaps
             longer than ``IDLE_AFTER_S`` don't count, so rates stay honest)
* paused   - manual pause; nothing is recorded

Leaving the map (or pressing restart) saves the session to disk and goes back
to waiting.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .exp_tracker import ExpTracker, Reading
from .items import PriceList, QuickslotUsage, SlotItems, item_key, slot_name, spent
from .kills import KillCounter, LineTracker, Message, Mob, estimate_missed, parse_message

IDLE_AFTER_S = 60.0


def _add_use(c: Counter, key, n: int) -> None:
    """Add n uses (n < 0 corrects earlier ones); a count never goes below zero."""
    c[key] = max(0, c[key] + n)
    if not c[key]:
        del c[key]


@dataclass
class Session:
    map_name: str = ""
    mobs: tuple[Mob, ...] = ()
    exp: ExpTracker = field(default_factory=ExpTracker)
    level: int = 0
    exp_percent: float = 0.0
    mesos: int = 0
    loot: Counter[int] = field(default_factory=Counter)  # item id -> count
    # Items used: quickslot usage is derived from ``quickslots`` (see items.py);
    # ``manual_*`` are uses booked directly with ``on_use`` / ``on_use_other``.
    manual_used: Counter[int] = field(default_factory=Counter)  # item id -> count
    manual_used_other: Counter[str] = field(default_factory=Counter)  # name -> count
    quickslots: QuickslotUsage = field(default_factory=QuickslotUsage)
    slot_items: SlotItems | None = None  # icon -> item, to name quickslot usage
    prices: PriceList | None = None  # shop buy prices, for mesos spent
    loot_unknown: Counter[str] = field(default_factory=Counter)
    names: dict[tuple[str, int], str] = field(default_factory=dict)
    killer: KillCounter = field(default_factory=KillCounter)
    message_exp: int = 0  # EXP (incl. party bonus) from messages we read
    party_bonus_exp: int = 0
    item_resolver: Callable[[str], int | None] | None = None
    idle_after: float = IDLE_AFTER_S

    started_at: datetime = field(default_factory=datetime.now)
    active_s: float = 0.0  # time spent actually grinding
    _last_tick: float | None = None
    _last_gain: float | None = None  # monotonic time of last EXP gain
    paused: bool = False

    def __post_init__(self) -> None:
        self.killer.set_map(self.mobs)
        for m in self.mobs:
            self.names[("mob", m.id)] = m.name

    # --- clock -------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._last_tick is not None and not self.paused and not self.is_idle(now):
            self.active_s += now - self._last_tick
        self._last_tick = now

    def is_idle(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self._last_gain is None or now - self._last_gain > self.idle_after

    def _gained(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.tick(now)
        self._last_gain = now

    def pause(self) -> None:
        self.tick()
        self.paused = True

    def resume(self) -> None:
        self.paused = False
        self._last_tick = time.monotonic()
        self.quickslots.new_segment()  # never diff stack counts across a pause
        self.exp.reset_baseline()  # EXP gained while paused doesn't count

    # --- inputs --------------------------------------------------------------

    def on_exp(self, level: int, exp: int, percent: float, now: float | None = None) -> None:
        if self.paused:
            return
        self.level, self.exp_percent = level, percent
        # The bar shows "exp [pct%]", so EXP needed for this level = exp / pct.
        if percent >= 5 and level not in self.exp.exp_table:
            self.exp.exp_table = {**self.exp.exp_table, level: round(exp * 100 / percent)}
        self.tick(now)
        # Timestamps on the active clock: idle time never dilutes the rate.
        if self.exp.add(Reading(self.active_s, level, exp)) > 0:
            self._gained(now)

    def on_message(self, msg: Message, now: float | None = None) -> None:
        if self.paused:
            return
        if msg.kind == "exp":
            self._gained(now)
            self.killer.add(msg.value)
            self.message_exp += msg.value
        elif msg.kind == "party_bonus":
            self.killer.mark_last_party()
            self.message_exp += msg.value
            self.party_bonus_exp += msg.value
        elif msg.kind == "mesos":
            self.on_mesos(msg.value)
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
        """Items used; n < 0 corrects uses booked earlier (never below zero)."""
        _add_use(self.manual_used, item_id, n)
        if name:
            self.names[("item", item_id)] = name

    def on_use_other(self, name: str, n: int = 1) -> None:
        """Usage of an item we have no id for (unidentified slot, or name-only)."""
        _add_use(self.manual_used_other, name, n)

    def on_quickslots(self, t: int, reads) -> None:
        """One tick of quickslot reads, ``(count, signature, since)`` per slot."""
        if not self.paused:
            self.quickslots.record(t, reads)

    def finish(self) -> None:
        """The session ended: freeze items used."""
        self.quickslots.finalise()

    def _quickslot_used(self) -> tuple[Counter[int], Counter[str]]:
        ids, other = Counter(), Counter()
        for item, n in self.quickslots.uses():
            known = next(filter(None, (self.slot_items.identify(s) for s in (item.signature, *item.aliases))),
                         None) if self.slot_items else None
            if known and known.item_id is not None:
                ids[known.item_id] += n
                self.names[("item", known.item_id)] = known.name
            else:
                other[known.name if known else slot_name(item.slot)] += n
        return ids, other

    @property
    def used(self) -> Counter[int]:
        """Items used per item id."""
        return self.manual_used + self._quickslot_used()[0]

    @property
    def used_other(self) -> Counter[str]:
        """Items used with no item id: by name, or "Slot N (key)" until the icon is named."""
        return self.manual_used_other + self._quickslot_used()[1]

    def on_mesos(self, amount: int) -> None:
        if not self.paused:
            self.mesos += amount

    # --- mesos ---------------------------------------------------------------

    def used_by_key(self) -> dict[str, int]:
        """Items used keyed like the price list (item id, else name)."""
        ids, other = self.used, self.used_other
        out = {item_key(k, ""): n for k, n in ids.items()}
        for name, n in other.items():
            out[name] = out.get(name, 0) + n
        return out

    def _spent(self) -> tuple[int, int]:
        names = {item_key(k, ""): self.names.get(("item", k), "") for k in self.used}
        return spent(self.used_by_key(), self.prices or PriceList(), names)

    @property
    def mesos_spent(self) -> int:
        """Mesos spent on used items, at the user's buy prices."""
        return self._spent()[0]

    @property
    def unpriced_used(self) -> int:
        """Items used that have no price yet (not counted in mesos_spent)."""
        return self._spent()[1]

    @property
    def net_mesos(self) -> int:
        return self.mesos - self.mesos_spent

    def net_mesos_per_hour(self) -> float:
        return self.net_mesos / self.active_s * 3600 if self.active_s > 0 else 0.0

    # --- derived -------------------------------------------------------------

    @property
    def kills(self) -> Counter[int]:
        """Party kills per mob id (identified only)."""
        c = self.killer.counts()
        c.pop(None, None)
        return c

    @property
    def total_kills(self) -> int:
        return self.killer.total

    @property
    def missed_kills(self) -> int:
        return estimate_missed(self.message_exp, self.exp.gained, self.total_kills)

    def rate_per_hour(self) -> float:
        return self.exp.rate_per_hour()

    def session_rate_per_hour(self) -> float:
        return self.exp.net / self.active_s * 3600 if self.active_s > 0 else 0.0

    def seconds_to_level(self) -> float | None:
        return self.exp.seconds_to_level()

    def has_data(self) -> bool:
        return self.exp.gained > 0 or self.total_kills > 0

    def to_dict(self) -> dict:
        def named(kind: str, c: Counter) -> dict[str, int]:
            return {self.names.get((kind, k), str(k)): n for k, n in c.items()}

        def entries(kind: str, c: Counter) -> list[dict]:
            return [{"id": k, "name": self.names.get((kind, k), str(k)), "count": n}
                    for k, n in c.most_common()]

        return {
            "map": self.map_name,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "active_seconds": round(self.active_s),
            "level": self.level,
            "exp_gained": self.exp.gained,
            "exp_lost": self.exp.lost,
            "levels_gained": self.exp.levels_gained,
            "exp_per_hour": round(self.session_rate_per_hour()),
            "party_kills": self.total_kills,
            "kills": named("mob", self.kills),
            "loot": {**named("item", self.loot), **dict(self.loot_unknown)},
            "used": {**named("item", self.used), **dict(self.used_other)},
            "mesos": self.mesos,
            "mesos_spent": self.mesos_spent,
            "net_mesos": self.net_mesos,
            "net_mesos_per_hour": round(self.net_mesos_per_hour()),
            "used_unpriced": self.unpriced_used,
            # Same as kills/loot/used but with ids, for sprites and icons in the history view.
            "kill_list": entries("mob", self.kills),
            "loot_list": entries("item", self.loot) + [
                {"id": None, "name": n, "count": c} for n, c in self.loot_unknown.items()
            ],
            "used_list": entries("item", self.used) + [
                {"id": None, "name": n, "count": c} for n, c in self.used_other.most_common()
            ],
            # Raw "You received EXP" values (solo/party) to check monster matching.
            "exp_values": {
                "solo": dict(Counter(v for v, p in zip(self.killer.values, self.killer.party) if not p)),
                "party": dict(Counter(v for v, p in zip(self.killer.values, self.killer.party) if p)),
            },
        }


def sessions_dir() -> Path:
    from .paths import sub

    return sub("sessions")


@dataclass
class Tracker:
    item_resolver: Callable[[str], int | None] | None = None
    save_dir: Path | None = None  # None -> default sessions dir
    exp_table: dict[int, int] = field(default_factory=dict)

    session: Session | None = None
    last: Session | None = None  # most recently finished session
    paused: bool = False
    map_name: str = ""
    mobs: tuple[Mob, ...] = ()
    lines: LineTracker = field(default_factory=LineTracker)
    _status: tuple[int, int, float] | None = None
    saved: list[Path] = field(default_factory=list)
    on_saved: list[Callable[[Path], None]] = field(default_factory=list)
    _today: dict = field(default_factory=dict)  # totals of sessions saved today
    prices: PriceList = field(default_factory=PriceList)
    slot_items: SlotItems = field(default_factory=SlotItems)
    slots: list = field(default_factory=list)  # latest quickslot reads, for display
    _qs_tick: int = 0  # quickslot reads so far: the clock for usage series

    def __post_init__(self) -> None:
        self._load_today()

    @property
    def state(self) -> str:
        if self.paused:
            return "paused"
        return "active" if self.session else "waiting"

    @property
    def combat_map(self) -> bool:
        return bool(self.mobs)

    @property
    def view(self) -> Session | None:
        """Session to display: current, else the last finished one."""
        return self.session or self.last

    # --- inputs -----------------------------------------------------------------

    def on_map(self, name: str, mobs: Sequence[Mob] = ()) -> None:
        if name == self.map_name:
            return
        self.end_session()
        self.map_name, self.mobs = name, tuple(mobs)

    def on_status(self, level: int, exp: int, percent: float, now: float | None = None) -> None:
        prev = self._status
        self._status = (level, exp, percent)
        if self.paused:
            return
        if self.session is None:
            gained = prev is not None and (level, exp) > (prev[0], prev[1])
            if gained and self.combat_map:
                self._start(prev, now)
            else:
                return
        self.session.on_exp(level, exp, percent, now)

    def on_message_lines(self, visible_lines, now: float | None = None) -> None:
        for line in self.lines.update(visible_lines):  # always consume lines
            msg = parse_message(line)
            if msg is None or self.paused:
                continue
            if self.session is None:
                if msg.kind == "exp" and self.combat_map:
                    self._start(self._status, now)
                else:
                    continue
            self.session.on_message(msg, now)

    def on_quickslots(self, reads) -> None:
        """Per-slot reads (``SlotRead``) from the quickslot panel, ~1/s.

        Only an active, unpaused session records them; each session starts
        from an empty series, so nothing carries over from waiting or pauses.
        """
        self.slots = list(reads)
        self._qs_tick += 1
        if self.session is not None and not self.paused:
            self.session.on_quickslots(self._qs_tick, [
                (r.count, r.signature, getattr(r, "since", 0)) for r in self.slots])

    # --- controls ---------------------------------------------------------------

    def pause(self) -> None:
        self.paused = True
        if self.session:
            self.session.pause()

    def resume(self) -> None:
        self.paused = False
        if self.session:
            self.session.resume()

    def toggle_pause(self) -> None:
        self.resume() if self.paused else self.pause()

    def restart(self) -> None:
        self.end_session()
        self.paused = False

    def end_session(self) -> Path | None:
        s, self.session = self.session, None
        if s is not None:
            s.finish()
        if s is None or not s.has_data():
            return None
        s.tick()
        self.last = s
        self.exp_table.update(s.exp.exp_table)
        d = self.save_dir or sessions_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{s.started_at:%Y%m%d-%H%M%S}.json"
        rec = s.to_dict()
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        self.saved.append(path)
        if path.name.startswith(self._today.get("date", "")):
            self._add_today(rec)
        for cb in self.on_saved:
            cb(path)
        return path

    def tick(self) -> None:
        if self.session:
            self.session.tick()

    # --- today's totals -----------------------------------------------------------

    def _load_today(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        self._today = {"date": today, "exp": 0, "kills": 0, "seconds": 0, "sessions": 0,
                       "mesos": 0, "mesos_spent": 0}
        d = self.save_dir or sessions_dir()
        for f in sorted(d.glob(f"{today}-*.json")) if d.exists() else []:
            try:
                self._add_today(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue

    def _add_today(self, rec: dict) -> None:
        self._today["exp"] += rec.get("exp_gained", 0)
        self._today["kills"] += rec.get("party_kills", 0)
        self._today["seconds"] += rec.get("active_seconds", 0)
        self._today["sessions"] += 1
        self._today["mesos"] += rec.get("mesos", 0)
        self._today["mesos_spent"] += rec.get("mesos_spent", 0)

    def today(self) -> dict:
        """Today's totals: saved sessions plus the current one."""
        if self._today.get("date") != datetime.now().strftime("%Y%m%d"):
            self._load_today()  # crossed midnight
        t = dict(self._today)
        if self.session:
            t["exp"] += self.session.exp.gained
            t["kills"] += self.session.total_kills
            t["seconds"] += round(self.session.active_s)
            t["sessions"] += 1
            t["mesos"] += self.session.mesos
            t["mesos_spent"] += self.session.mesos_spent
        t["net_mesos"] = t["mesos"] - t["mesos_spent"]
        return t

    def _start(self, baseline: tuple[int, int, float] | None, now: float | None) -> None:
        s = Session(map_name=self.map_name, mobs=self.mobs, item_resolver=self.item_resolver,
                    prices=self.prices, slot_items=self.slot_items)
        s.exp.exp_table = dict(self.exp_table)
        s.tick(now)
        if baseline:  # the reading just before the first gain
            s.level, s.exp_percent = baseline[0], baseline[2]
            s.exp.add(Reading(0.0, baseline[0], baseline[1]))
        self.session = s
