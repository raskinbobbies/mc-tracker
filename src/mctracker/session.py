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
from .items import AppSettings, PriceList, QuickslotUsage, SlotItems, item_key, spent
from .kills import BarKills, KillCounter, LineDiff, Message, Mob, split

IDLE_AFTER_S = 60.0
# A "Bonus EXP for PARTY" line means you're in a party with someone on your
# map: kills are counted from party lines (not solo EXP values) for this long
# after one. Short, so a party change or leaving shows up fast.
PARTY_KILL_HOLD_S = 10.0
RECENT_GAINS = 120  # gains remembered for attributing corrections


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
    digits_level: int = 0  # level digits as last read (shown until one validates)
    raw_percent: float = 0.0  # bar percent as last read
    mesos: int = 0
    loot: Counter[int] = field(default_factory=Counter)  # item id -> count
    # Items used: quickslot usage is derived from ``quickslots`` (see items.py);
    # ``manual_*`` are uses booked directly with ``on_use`` / ``on_use_other``.
    manual_used: Counter[int] = field(default_factory=Counter)  # item id -> count
    manual_used_other: Counter[str] = field(default_factory=Counter)  # name -> count
    quickslots: QuickslotUsage = field(default_factory=QuickslotUsage)
    slot_items: SlotItems | None = None  # slot -> item: only assigned slots are tracked
    prices: PriceList | None = None  # shop buy prices, for mesos spent
    # Experimental (off by default): items used and mesos spent are estimates. While
    # off, nothing is recorded or shown for them and net mesos = mesos picked up.
    items_used: bool = False
    loot_unknown: Counter[str] = field(default_factory=Counter)
    names: dict[tuple[str, int], str] = field(default_factory=dict)
    killer: KillCounter = field(default_factory=KillCounter)  # sampled EXP lines: the mix
    bar: BarKills = field(default_factory=BarKills)  # kills from the EXP bar: the magnitude
    party_seen: bool = False  # a party bonus line this session
    _bonus_t: float | None = None  # last party bonus line
    message_exp: int = 0  # EXP (incl. party bonus) from messages we read
    party_bonus_exp: int = 0
    item_resolver: Callable[[str], int | None] | None = None
    idle_after: float = IDLE_AFTER_S

    started_at: datetime = field(default_factory=datetime.now)
    active_s: float = 0.0  # time spent actually grinding
    _last_tick: float | None = None
    _last_gain: float | None = None  # monotonic time of last EXP gain
    paused: bool = False
    # EXP gained per active minute (signed: a correction can take some back)
    _minutes: list[int] = field(default_factory=list)
    # Recent gains as [minute, amount], newest last: a correction is taken
    # back from the minute that booked the bad gain.
    _recent_gains: list[list[int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.killer.set_map(self.mobs)
        self.killer.solo = not self.party_seen  # solo until a party bonus line shows
        for m in self.mobs:
            self.names[("mob", m.id)] = m.name

    @property
    def level(self) -> int:
        """The tracker's committed (solved) level; the raw digits before that."""
        lvl = self.exp.level
        return lvl if lvl is not None else self.digits_level

    @property
    def exp_percent(self) -> float:
        """Percent of the committed reading, so it always matches ``level``."""
        cur = self.exp.last
        return cur.pct if cur is not None and cur.pct is not None else self.raw_percent

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
        # EXP gained while paused doesn't count.
        self._book(self.exp.reset_baseline())

    def finalize(self, now: float | None = None) -> None:
        """Book what trailing unvalidated readings safely show (session ending)."""
        self._book(self.exp.finalize(), now)

    # --- inputs --------------------------------------------------------------

    def on_exp(self, level: int | None, exp: int, percent: float | None, now: float | None = None) -> None:
        if self.paused:
            return
        if level:
            self.digits_level = level
        if percent is not None:
            self.raw_percent = percent
        self.tick(now)
        # Timestamps on the active clock: idle time never dilutes the rate.
        # ``now`` (monotonic, like message timestamps) stamps last_committed_t.
        mono = time.monotonic() if now is None else now
        delta = self.exp.add(Reading(self.active_s, level, exp, percent), now=mono)
        if undone := self.exp.undone:  # take the bad step back, book the rest now
            self._book(-undone, now)
            delta += undone
        self._book(delta, now)
        self._count_kills(mono)

    def _count_kills(self, now: float) -> None:
        """Convert new EXP-bar gain to kills (see ``BarKills``)."""
        e = self.exp
        # The kill cap documented in exp_tracker: never above booked EXP.
        gain = e.verified_gain + e.unconfirmed_gain - e.retracted
        party = self._party_now(now)
        self.bar.update(gain, now, () if party else self._solo_values(), party=party, hold=e.behind)

    def _party_now(self, now: float) -> bool:
        return self._bonus_t is not None and now - self._bonus_t < PARTY_KILL_HOLD_S

    def _solo_values(self) -> set[int]:
        """Per-kill EXP values trusted for decomposing solo gain: read at least
        twice, or matching a map monster's EXP (at the fitted scale). Values
        read while in a party are shares, never solo values."""
        k = self.killer
        k.classify(1)  # fit the scale
        mobs = [m.exp * k.scale for m in k.mobs]
        return {v for v, n in Counter(v for v, p in zip(k.values, k.party) if not p).items()
                if n >= 2 or any(abs(v - x) <= 0.04 * x for x in mobs)}

    def _book(self, delta: int, now: float | None = None) -> None:
        if delta > 0:
            self._gained(now)
            i = max(0, int(self.active_s // 60))
            self._minutes.extend([0] * (i + 1 - len(self._minutes)))
            self._minutes[i] += delta
            self._recent_gains.append([i, delta])
            del self._recent_gains[:-RECENT_GAINS]
        elif delta < 0:  # a misread corrected: take it back where it was booked
            left = -delta
            while left and self._recent_gains:
                g = self._recent_gains[-1]
                take = min(left, g[1])
                self._minutes[g[0]] -= take
                g[1] -= take
                left -= take
                if g[1] == 0:
                    self._recent_gains.pop()
            if left and self._minutes:
                self._minutes[-1] -= left

    def on_message(self, msg: Message, now: float | None = None) -> None:
        if self.paused:
            return
        mono = time.monotonic() if now is None else now
        if msg.kind == "exp":
            self._gained(now)
            self.killer.add(msg.value, party=self._party_now(mono))
            self.message_exp += msg.value
            self.bar.line(mono, msg.value)
            if not self._party_now(mono):  # in a party, wait for its bonus line (on_lines)
                self._count_kills(mono)  # the bar may have moved before the line showed
        elif msg.kind == "party_bonus":
            self._bonus_t = mono
            self.party_seen = True
            self.killer.solo = False
            self.killer.mark_last_party()
            self.bar.bonus(msg.value)
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

    def on_lines(self, msgs: Sequence[Message], now: float | None = None) -> None:
        """One frame's new messages: learn EXP per party kill from clean
        kill + party-bonus pairs (a kill line directly followed by its bonus)."""
        for a, b in zip(msgs, msgs[1:]):
            if a.kind == "exp" and b.kind == "party_bonus":
                self.bar.pair(a.value + b.value)
        if any(m.kind == "exp" for m in msgs):
            self._count_kills(time.monotonic() if now is None else now)

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
        """Usage of an item we have no id for (name-only)."""
        _add_use(self.manual_used_other, name, n)

    def on_quickslots(self, t: int, reads) -> None:
        """One tick of quickslot reads, ``(count, signature, since)`` per slot."""
        if self.items_used and not self.paused:
            self.quickslots.record(t, reads, self.slot_items)

    def finish(self) -> None:
        """The session ended: freeze items used."""
        self.quickslots.finalise()

    def set_items_used(self, on: bool) -> None:
        """Switch the items-used estimate on or off for this session."""
        if on and not self.items_used:
            self.quickslots.new_segment()  # nothing was read while off: don't diff across it
        self.items_used = on

    def _quickslot_used(self) -> tuple[Counter[int], Counter[str]]:
        ids, other = Counter(), Counter()
        if not self.items_used:
            return ids, other
        for item, n in self.quickslots.uses():
            if item.item_id is not None:
                ids[item.item_id] += n
                self.names[("item", item.item_id)] = item.name
            else:
                other[item.name] += n
        return ids, other

    @property
    def used(self) -> Counter[int]:
        """Items used per item id."""
        if not self.items_used:
            return Counter()
        return self.manual_used + self._quickslot_used()[0]

    @property
    def used_other(self) -> Counter[str]:
        """Items used with no item id, by name."""
        if not self.items_used:
            return Counter()
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
        if not self.items_used:
            return 0, 0
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
        """Kills per mob id: each counted line's own monster; kills with no line
        get each monster's share of the sampled lines, rounded down."""
        k, counted = self.killer, self.bar.counted
        c = Counter()
        for (v, party), n in Counter(counted).items():
            c[k.identify(v, party)] += n
        c.update(split(k.counts(), self.total_kills - len(counted)))
        c.pop(None, None)
        return c

    @property
    def unassigned_kills(self) -> int:
        """Kills the sampled lines can't attribute to a monster."""
        return self.total_kills - sum(self.kills.values())

    @property
    def total_kills(self) -> int:
        """Kills converted from EXP-bar gain (never more than the bar supports)."""
        return self.bar.total

    @property
    def kills_read(self) -> int:
        """EXP lines read (a sample: some are missed, a few may repeat)."""
        return self.killer.total

    def rate_per_hour(self) -> float:
        return self.exp.rate_per_hour()

    def session_rate_per_hour(self) -> float:
        return self.exp.net / self.active_s * 3600 if self.active_s > 0 else 0.0

    def seconds_to_level(self) -> float | None:
        return self.exp.seconds_to_level()

    @property
    def exp_per_minute(self) -> list[int]:
        """EXP gained in each active minute (idle and paused time excluded)."""
        n = int(self.active_s // 60) + 1 if self._minutes else 0
        return [max(0, m) for m in self._minutes] + [0] * (n - len(self._minutes))

    def best_minute(self) -> int:
        return max(self.exp_per_minute, default=0)

    def has_data(self) -> bool:
        return self.exp.gained > 0 or self.total_kills > 0 or self.kills_read > 0

    def to_dict(self) -> dict:
        def named(kind: str, c: Counter) -> dict[str, int]:
            return {self.names.get((kind, k), str(k)): n for k, n in c.items()}

        def entries(kind: str, c: Counter) -> list[dict]:
            return [{"id": k, "name": self.names.get((kind, k), str(k)), "count": n}
                    for k, n in c.most_common()]

        est = self.items_used
        out = {
            "map": self.map_name,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "active_seconds": round(self.active_s),
            "level": self.level,
            "exp_gained": self.exp.gained,
            "exp_lost": self.exp.lost,
            "levels_gained": self.exp.levels_gained,
            "exp_per_hour": round(self.session_rate_per_hour()),
            "exp_per_minute": self.exp_per_minute,
            "party_kills": self.total_kills,
            "kills_read": self.kills_read,
            "unassigned_kills": self.unassigned_kills,
            "quarantined_exp": self.bar.quarantined,
            "kills": named("mob", self.kills),
            "loot": {**named("item", self.loot), **dict(self.loot_unknown)},
            "mesos": self.mesos,
            "net_mesos": self.net_mesos,
            "net_mesos_per_hour": round(self.net_mesos_per_hour()),
            # Same as kills/loot/used but with ids, for sprites and icons in the history view.
            "kill_list": entries("mob", self.kills),
            "loot_list": entries("item", self.loot) + [
                {"id": None, "name": n, "count": c} for n, c in self.loot_unknown.items()
            ],
            # Raw "You received EXP" values (solo/party) to check monster matching.
            "exp_values": {
                "solo": dict(Counter(v for v, p in zip(self.killer.values, self.killer.party) if not p)),
                "party": dict(Counter(v for v, p in zip(self.killer.values, self.killer.party) if p)),
            },
        }
        if est:  # experimental: every figure below is an estimate
            out |= {
                "items_used_estimate": True,
                "used": {**named("item", self.used), **dict(self.used_other)},
                "mesos_spent": self.mesos_spent,
                "used_unpriced": self.unpriced_used,
                "used_list": entries("item", self.used) + [
                    {"id": None, "name": n, "count": c} for n, c in self.used_other.most_common()
                ],
            }
        return out


def sessions_dir() -> Path:
    from .paths import sub

    return sub("sessions")


@dataclass
class Tracker:
    item_resolver: Callable[[str], int | None] | None = None
    save_dir: Path | None = None  # None -> default sessions dir
    # EXP sizes learned from the bar, and table levels found wrong, kept
    # across sessions.
    learned_exp: dict[int, int] = field(default_factory=dict)
    distrusted: set[int] = field(default_factory=set)

    session: Session | None = None
    last: Session | None = None  # most recently finished session
    paused: bool = False
    map_name: str = ""
    mobs: tuple[Mob, ...] = ()
    lines: LineDiff = field(default_factory=LineDiff)  # system messages area
    _status: tuple[int, int, float | None] | None = None
    # Follows the bar while no session runs, so a session starts only on a
    # validated rise, from a validated baseline.
    _probe: ExpTracker | None = None
    saved: list[Path] = field(default_factory=list)
    on_saved: list[Callable[[Path], None]] = field(default_factory=list)
    _today: dict = field(default_factory=dict)  # totals of sessions saved today
    prices: PriceList = field(default_factory=PriceList)
    slot_items: SlotItems = field(default_factory=SlotItems)
    settings: AppSettings = field(default_factory=AppSettings)
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
    def items_used(self) -> bool:
        """Whether the experimental items-used estimate is on."""
        return self.settings.experimental_items_used

    def set_items_used(self, on: bool) -> None:
        """Turn the experimental items-used estimate on or off, and remember the choice.

        The new value applies even if saving fails (the error is raised after).
        """
        self.settings.experimental_items_used = on
        if self.session is not None:
            self.session.set_items_used(on)
        if not on:
            self.slots = []
        self.settings.save()

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

    def on_status(self, level: int | None, exp: int, percent: float | None, now: float | None = None) -> None:
        self._status = (level, exp, percent)
        if self.paused:
            return
        if self.session is None:
            probe = self._get_probe()
            net = probe.net
            probe.add(Reading(time.monotonic() if now is None else now, level, exp, percent))
            if probe.net > net and self.combat_map:
                self._start(probe.previous, now)  # the committed reading before the rise
            else:
                return
        self.session.on_exp(level, exp, percent, now)

    def _get_probe(self) -> ExpTracker:
        if self._probe is None:
            self._probe = ExpTracker(learned=dict(self.learned_exp), distrusted=set(self.distrusted))
        return self._probe

    def on_message_lines(self, visible_lines, now: float | None = None) -> None:
        msgs = self.lines.update(visible_lines)  # always consume lines
        for msg in msgs:
            if self.paused:
                continue
            if self.session is None:
                if msg.kind == "exp" and self.combat_map:
                    self._start(self._probe.last if self._probe else None, now)
                else:
                    continue
            self.session.on_message(msg, now)
        if self.session is not None and not self.paused:
            self.session.on_lines(msgs, now)

    def on_quickslots(self, reads) -> None:
        """Per-slot reads (``SlotRead``) from the quickslot panel, ~1/s.

        Only an active, unpaused session records them; each session starts
        from an empty series, so nothing carries over from waiting or pauses.
        """
        if not self.items_used:
            return
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
        elif self._probe is not None:
            self._probe.reset_baseline()  # EXP earned while paused never starts a session

    def toggle_pause(self) -> None:
        self.resume() if self.paused else self.pause()

    def restart(self) -> None:
        self.end_session()
        self.paused = False

    def end_session(self) -> Path | None:
        s, self.session = self.session, None
        if s is None:
            return None
        s.finalize()
        s.finish()
        # The session started from a copy of these; it may have learned or restored entries.
        self.learned_exp = dict(s.exp.learned)
        self.distrusted = set(s.exp.distrusted)
        self._probe = None
        if s.exp.last is not None and not self.paused:
            # Keep following from where it ended. Not while paused: that reading
            # is stale, and EXP earned since must not book into the next session.
            self._get_probe().seed(s.exp.last)
        if not s.has_data():
            return None
        s.tick()
        self.last = s
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
        if not self.items_used:  # the estimate is off: ignore what earlier sessions recorded
            t["mesos_spent"] = 0
        t["net_mesos"] = t["mesos"] - t["mesos_spent"]
        return t

    def _start(self, baseline: Reading | None, now: float | None) -> None:
        if self._probe is not None:  # what the probe learned while waiting
            self.learned_exp = dict(self._probe.learned)
            self.distrusted = set(self._probe.distrusted)
            self._probe = None
        s = Session(map_name=self.map_name, mobs=self.mobs, item_resolver=self.item_resolver,
                    prices=self.prices, slot_items=self.slot_items, items_used=self.items_used)
        s.exp.learned = dict(self.learned_exp)
        s.exp.distrusted = set(self.distrusted)
        s.tick(now)
        if baseline is not None:  # validated reading just before the first gain
            s.digits_level = baseline.level
            if baseline.pct is not None:
                s.raw_percent = baseline.pct
            s.exp.seed(
                Reading(0.0, baseline.level, baseline.exp, baseline.pct),
                now=time.monotonic() if now is None else now,
            )
        self.session = s
