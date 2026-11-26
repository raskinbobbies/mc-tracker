"""Grinding sessions: one per map visit, started by the first EXP gain.

``Tracker`` owns the lifecycle:

* waiting  - no session; on a map with monsters, the first EXP gain starts one
* active   - tracking; session time is the wall clock from the first EXP gain,
             stopped only while paused. AVG/min, EXP/hour, ETA and the per-minute
             history all use it (idle minutes show as 0), like maplestory.watch.
             Past ``IDLE_AFTER_S`` without a gain the session is "idle": a status
             only, and the current rate reads 0.
* paused   - manual pause; nothing is recorded

Leaving the map (or pressing restart) saves the session to disk and goes back
to waiting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from .exp_tracker import ExpTracker, Reading
from .items import AppSettings, PriceList, QuickslotUsage, SlotItems, item_key, spent, write_atomic
from .kills import BarKills, KillCounter, LineDiff, Message, Mob, colour_allows_party

log = logging.getLogger(__name__)

IDLE_AFTER_S = 60.0
# The clock ticks about once a second; a longer gap between ticks is the PC asleep or hibernating
# (or the app frozen): not session time.
SUSPEND_GAP_S = 30.0
AUTOSAVE_S = 30.0  # the running session is written to CURRENT_FILE this often
CURRENT_FILE = ".current.json"  # a dot file: history and today's totals never read it as a saved session
SCHEMA_VERSION = 1  # saved session files
CURRENT_RATE_S = 60.0  # the headline rate is the EXP of this trailing wall-clock window
# A "Bonus EXP for PARTY" line means you're in a party with someone on your
# map: kills are counted as party kills from then on while the party size is
# above 1, until the party widget shows no one else (``party_widget_alone``)
# or this many kill lines in a row come without a bonus line. With no party
# (size 1, e.g. no widget and the size not raised), only for
# PARTY_KILL_HOLD_S after a bonus line.
PARTY_KILL_HOLD_S = 10.0
PARTY_SOLO_LINES = 3
PARTY_BONUS_WAIT_S = 1.0  # a kill line's bonus line comes in this soon, if at all
# Party size (the label and solo/party mix) trusts a bonus line over an empty
# party widget read for this long.
PARTY_SIZE_HOLD_S = 60.0
# The party widget can miss a member for a read (very low HP, a dead member,
# something drawn over it), so a smaller party is only believed after this
# many consecutive widget reads (~3 s apart) agree. Party messages ("left the
# party", "disbanded") lower it at once.
PARTY_DROP_READS = 3
# A larger party from the widget needs this many consecutive reads too: one
# misread (something red or framed drawn over the play field) must not label
# a solo session as a party. Party messages raise it at once.
PARTY_RAISE_READS = 2
# ... and a new widget count must also have held this long (first to last of
# those reads), whatever the read rate: a one-frame miss must never change
# the party (#29). Same span as leaving party mode below.
PARTY_SETTLE_S = 1.0
# Kills are solo once widget reads this far apart (two or more) show no one else.
PARTY_ALONE_S = 1.0
# After leaving a party, ignore widget reads and bonus lines still on screen.
PARTY_LEFT_HOLD_S = 6.0
RECENT_GAINS = 120  # gains remembered for attributing corrections


def party_label(size: int) -> str:
    return "Solo" if size <= 1 else f"Party of {size}"


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
    start_at: tuple[int, float] | None = None  # (level, bar percent) of the first committed reading
    mesos: int = 0
    loot: Counter[int] = field(default_factory=Counter)  # item id -> count
    # Items used: quickslot usage is derived from ``quickslots`` (see items.py);
    # ``manual_*`` are uses booked directly with ``on_use`` / ``on_use_other``.
    manual_used: Counter[int] = field(default_factory=Counter)  # item id -> count
    manual_used_other: Counter[str] = field(default_factory=Counter)  # name -> count
    quickslots: QuickslotUsage = field(default_factory=QuickslotUsage)
    slot_items: SlotItems | None = None  # slot -> item: only assigned slots are tracked
    prices: PriceList | None = None  # shop buy prices, for mesos spent
    loot_unknown: Counter[str] = field(default_factory=Counter)
    names: dict[tuple[str, int], str] = field(default_factory=dict)
    killer: KillCounter = field(default_factory=KillCounter)  # sampled EXP lines: the mix
    bar: BarKills = field(default_factory=BarKills)  # kills from the EXP bar: the magnitude
    party_size: int = 1  # 1 = solo
    party_max: int = 1  # largest party during the session
    party_seen: bool = False  # a party bonus line: some kills were counted as party
    _bonus_t: float | None = None  # last party bonus line; None: kills are solo
    _kill_lines: deque = field(default_factory=lambda: deque(maxlen=PARTY_SOLO_LINES))  # [time, bonus seen]
    _levels: int = 0  # levels gained at the last bar update
    message_exp: int = 0  # EXP (incl. party bonus) from messages we read
    party_bonus_exp: int = 0
    item_resolver: Callable[[str], int | None] | None = None
    idle_after: float = IDLE_AFTER_S

    started_at: datetime = field(default_factory=datetime.now)
    active_s: float = 0.0  # session wall time: since the first gain, minus paused time (idle counts)
    grind_s: float = 0.0  # time spent actually grinding (idle gaps excluded): the EXP tracker's clock
    _last_tick: float | None = None
    _last_gain: float | None = None  # monotonic time of last EXP gain
    paused: bool = False
    # EXP gained per active minute (signed: a correction can take some back)
    _minutes: list[int] = field(default_factory=list)
    # Recent gains as [minute, amount], newest last: a correction is taken
    # back from the minute that booked the bad gain.
    _recent_gains: list[list[int]] = field(default_factory=list)
    # (wall time, signed EXP) of the last CURRENT_RATE_S, for the current rate
    _rate_log: deque = field(default_factory=deque)

    def refine_map(self, name: str, mobs: Sequence[Mob]) -> None:
        """The same map visit, now resolved: its name and mobs, nothing reset."""
        self.map_name, self.mobs = name, tuple(mobs)
        self.killer.set_map(self.mobs)
        for m in self.mobs:
            self.names[("mob", m.id)] = m.name

    def __post_init__(self) -> None:
        self.killer.set_map(self.mobs)
        self.killer.solo = self.party_size == 1
        self.set_party_size(self.party_size)
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
        if self._last_tick is not None and not self.paused:
            gap = now - self._last_tick
            if gap <= SUSPEND_GAP_S:  # longer: asleep, not playing
                self.active_s += gap
                if not self.is_idle(now):
                    self.grind_s += gap
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

    def set_party_size(self, n: int) -> None:
        if max(1, n) != self.party_size:
            self.bar.boundary()  # a no-op for kills now (party kills price nothing), kept as the hook
        self.party_size = max(1, n)
        self.party_max = max(self.party_max, self.party_size)
        if self.party_size > 1:
            self.killer.solo = False  # never back to True: earlier lines stay party shares
        else:
            # Left or disbanded: no recent bonus line may keep counting kills as party.
            self._bonus_t = None

    def party_widget_alone(self) -> None:
        """The party widget shows no one else: kills are solo again."""
        if self._bonus_t is not None:
            self._solo_again()

    def _solo_again(self) -> None:
        # The kill lines since the last bonus line were solo kills: their
        # values are solo values.
        self._bonus_t = None
        k, n = self.killer, 0
        for _, paired in reversed(self._kill_lines):
            if paired:
                break
            n += 1
        for i in range(len(k.party) - n, len(k.party)):
            k.party[i] = False
        k._dirty = True

    def on_exp(self, level: int | None, exp: int, percent: float | None, now: float | None = None) -> None:
        if self.paused:
            return
        if level:
            self.digits_level = level
        if percent is not None:
            self.raw_percent = percent
        self.tick(now)
        # Timestamps on the grind clock (idle excluded): the tracker's spike windows and step caps
        # are tuned to it; the session figures use the wall clock (``active_s``).
        # ``now`` (monotonic, like message timestamps) stamps last_committed_t.
        mono = time.monotonic() if now is None else now
        delta = self.exp.add(Reading(self.grind_s, level, exp, percent), now=mono)
        self._note_start()
        if undone := self.exp.undone:  # take the bad step back, book the rest now
            self._book(-undone, now)
            delta += undone
        self._book(delta, now)
        self._count_kills(mono)

    def _note_start(self) -> None:
        """Remember where the bar stood at the first committed reading (saved as the level start)."""
        cur = self.exp.last
        if self.start_at is None and cur is not None and cur.pct is not None:
            self.start_at = (cur.level, cur.pct)

    def _count_kills(self, now: float) -> None:
        """Convert new EXP-bar gain to kills (see ``BarKills``)."""
        e = self.exp
        # The kill cap documented in exp_tracker: never above booked EXP.
        gain = e.verified_gain + e.unconfirmed_gain - e.retracted
        party = self._party_now(now)
        if party:  # steps: a step taken back is no kill (that sum never falls, see current_gain)
            gain = e.current_gain
        wrap, self._levels = e.levels_gained != self._levels, e.levels_gained
        self.bar.update(gain, now, () if party else self._solo_values(), party=party, hold=e.behind,
                        wrap=wrap, unconfirmed=e.unconfirmed_gain, bar_t=e.last_committed_t)

    def _party_now(self, now: float) -> bool:
        if self._bonus_t is None:
            return False
        k = self._kill_lines
        if len(k) == k.maxlen and not any(b for _, b in k) and now - k[-1][0] >= PARTY_BONUS_WAIT_S:
            self._solo_again()  # kill lines with no bonus line: we left the party
            return False
        # While in a party, slow kills stay party kills.
        return self.party_size > 1 or now - self._bonus_t < PARTY_KILL_HOLD_S

    def _solo_values(self) -> set[int]:
        """Per-kill EXP values trusted for decomposing solo gain: within ~10%
        of a map monster's EXP (at the fitted scale), or the whole bar gain of
        a lone line at least twice. Being read often proves nothing (a quest's
        EXP line repeats too). Values read while in a party are shares, never
        solo values."""
        k = self.killer
        k.classify(1)  # fit the scale
        mobs = [m.exp * k.scale for m in k.mobs]
        return {v for v in {v for v, p in zip(k.values, k.party) if not p}
                if self.bar.clean[v] >= 2 or any(abs(v - x) <= 0.1 * x for x in mobs)}

    def _book(self, delta: int, now: float | None = None) -> None:
        if delta:
            self._rate_log.append((self.active_s, delta))
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
            self._kill_lines.append([mono, False])
            self.message_exp += msg.value
            self.bar.line(mono, msg.value, msg.late, msg.doubt)
            if not self._party_now(mono):  # in a party, wait for its bonus line (on_lines)
                self._count_kills(mono)  # the bar may have moved before the line showed
        elif msg.kind == "party_bonus":
            self._bonus_t = mono
            if self._kill_lines:
                self._kill_lines[-1][1] = True
            self.party_seen = True
            self.killer.solo = False
            self.killer.mark_last_party()
            self.bar.bonus(msg.value, mono)
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
        """One frame's new messages: once they are all in, count the bar's
        kills (a party kill line waits for its bonus line)."""
        if any(m.kind == "exp" for m in msgs):
            self._count_kills(time.monotonic() if now is None else now)

    def on_loot(self, item_id: int, name: str = "", n: int = 1) -> None:
        self.loot[item_id] += n
        self.quickslots.on_loot(item_id, name, n)
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
        if not self.paused:
            self.quickslots.record(t, reads, self.slot_items)

    def finish(self) -> None:
        """The session ended: freeze items used."""
        self.quickslots.finalise()

    def _quickslot_used(self) -> tuple[Counter[int], Counter[str]]:
        ids, other = Counter(), Counter()
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
        return self.manual_used + self._quickslot_used()[0]

    @property
    def used_other(self) -> Counter[str]:
        """Items used with no item id, by name."""
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
        """Kills per mob id: each counted line's own monster. Kills with no line are
        never spread over the mobs by the sampled lines' shares (that can over-count a
        mob); they stay unassigned (undercount only)."""
        k = self.killer
        c = Counter()
        for (v, party), n in Counter(self.bar.counted).items():
            c[k.identify(v, party)] += n
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

    def rate_per_hour(self, now: float | None = None) -> float:
        """The current EXP/hr: the net EXP of the last ``CURRENT_RATE_S`` of wall time, so it falls
        smoothly to 0 within a minute of the last gain (like maplestory.watch). Negative right after a
        death. ``now`` (monotonic) adds the time since the last tick."""
        t = self.active_s
        if now is not None and self._last_tick is not None and not self.paused:
            t += max(0.0, now - self._last_tick)
        while self._rate_log and self._rate_log[0][0] <= t - CURRENT_RATE_S:
            self._rate_log.popleft()
        span = min(CURRENT_RATE_S, t)
        if span <= 0:
            return 0.0
        return sum(d for _, d in self._rate_log) / span * 3600

    def spent_summary(self) -> tuple[int, int]:
        """(mesos spent on items used, items used with no price), priced once for a refresh."""
        return self._spent()

    def session_rate_per_hour(self) -> float:
        """Net EXP over the session's wall time."""
        return self.exp.net / self.active_s * 3600 if self.active_s > 0 else 0.0

    def seconds_to_level(self) -> float | None:
        """Time to the next level at the session's wall-time average, else the current rate."""
        left = self.exp.exp_remaining()
        if left is None:
            return None
        rate = self.session_rate_per_hour()
        if rate <= 0:
            rate = self.rate_per_hour()
        return left / rate * 3600 if rate > 0 else None

    @property
    def exp_per_minute(self) -> list[int]:
        """EXP gained in each wall-clock minute of the session (idle minutes are 0; paused time is excluded)."""
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

        out = {
            "map": self.map_name,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "active_seconds": round(self.active_s),  # key kept: now wall time minus paused time
            "level": self.level,
            "exp_gained": self.exp.gained,
            "exp_lost": self.exp.lost,
            "levels_gained": self.exp.levels_gained,
            "exp_percent": round(self.exp_percent, 2),
            "exp_per_hour": round(self.session_rate_per_hour()),
            "exp_per_minute": self.exp_per_minute,
            "party_kills": self.total_kills,
            "kills_read": self.kills_read,
            "unassigned_kills": self.unassigned_kills,
            "quarantined_exp": self.bar.quarantined,
            "party_size": self.party_max,
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
        if self.start_at is not None:
            out["level_start"], out["exp_percent_start"] = self.start_at[0], round(self.start_at[1], 2)
        out |= {  # always saved, even when empty: a missing "used" means an old file
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


def _int(v) -> int:
    """A count from a saved file: null, text or garbage reads as 0."""
    if isinstance(v, bool):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return 0


def _stem(rec: dict) -> str:
    """File name (no suffix) for a session record: its start time to the second."""
    try:
        started = datetime.fromisoformat(rec["started_at"])
    except (KeyError, TypeError, ValueError):
        started = datetime.now()
    return f"{started:%Y%m%d-%H%M%S}"


def _free_path(d: Path, stem: str) -> Path:
    """``d/stem.json``, or ``stem-2.json``, ``-3``... when taken: two sessions can start in the same second."""
    path, n = d / f"{stem}.json", 2
    while path.exists():
        path, n = d / f"{stem}-{n}.json", n + 1
    return path


def _fmt_span(seconds: float) -> str:
    m = int(seconds) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else (f"{m}m" if m else f"{int(seconds)}s")


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
    channel: int | None = None  # read from the Change Channel dialog; None until seen, cleared when the same map reloads
    lines: LineDiff = field(default_factory=LineDiff)  # system messages area
    chat: LineDiff = field(default_factory=LineDiff)  # chat box: party join/leave
    party_size: int = 1  # 1 = solo
    _party_widget_seen: bool = False
    _party_bonus_t: float | None = None
    _party_left_t: float | None = None
    _party_low_reads: int = 0  # consecutive widget reads below party_size
    _party_low_size: int = 0  # the largest size those reads saw
    _party_high_reads: int = 0  # consecutive widget reads above party_size
    _party_high_size: int = 0  # the smallest size those reads saw
    _party_alone_t: float | None = None  # first of the widget reads in a row showing no one else
    _party_change_t: float = 0.0  # first of the widget reads in a row above or below party_size
    _status: tuple[int, int, float | None] | None = None
    # Follows the bar while no session runs, so a session starts only on a
    # validated rise, from a validated baseline.
    _probe: ExpTracker | None = None
    saved: list[Path] = field(default_factory=list)
    on_saved: list[Callable[[Path], None]] = field(default_factory=list)
    on_notice: list[Callable[[str], None]] = field(default_factory=list)  # a problem the user should see
    _pending: list[dict] = field(default_factory=list)  # ended sessions whose save failed: retried
    _autosave_t: float | None = None  # monotonic time of the last autosave
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
    def party_label(self) -> str:
        return party_label(self.party_size)

    @property
    def combat_map(self) -> bool:
        return bool(self.mobs)

    @property
    def view(self) -> Session | None:
        """Session to display: current, else the last finished one."""
        return self.session or self.last

    # --- inputs -----------------------------------------------------------------

    def on_map(self, name: str, mobs: Sequence[Mob] = (), refine: bool = False) -> None:
        """A confirmed map change ends the session (a new one starts on the next
        EXP gain). ``refine``: the same visit's raw read resolved to its map, so
        the session goes on with the map's mobs."""
        if name == self.map_name:
            return
        if refine and self.map_name:
            self.map_name, self.mobs = name, tuple(mobs)
            if self.session is not None:
                self.session.refine_map(name, mobs)
            return
        self.end_session()
        self.map_name, self.mobs = name, tuple(mobs)

    def on_channel(self, channel: int | None) -> None:
        """A channel read from the dialog; None clears it (the same map reloaded: a channel change).
        A portal to another map keeps it, and so does a new session."""
        self.channel = channel

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

    def game_closed(self) -> Path | None:
        """The game window went away: save the session like any other end, and forget the last reading.
        The clock is wall time, so a session kept open would grow while the game is closed, and after a
        restart the reads may be another character's: the next read starts from scratch."""
        path = self.end_session()
        self._probe = None  # end_session seeds it with the last reading; that character may not come back
        self._status = None
        self.paused = False  # a pause doesn't carry over to the next game: its reads would be ignored
        return path

    def _get_probe(self) -> ExpTracker:
        if self._probe is None:
            self._probe = ExpTracker(learned=dict(self.learned_exp), distrusted=set(self.distrusted))
        return self._probe

    def on_party(self, members: int, now: float | None = None) -> None:
        """Party widget read: its member count (0 = no widget)."""
        now = time.monotonic() if now is None else now
        if self._left_recently(now):
            return  # we just left: a widget still showing is stale
        if members:
            self._party_widget_seen = True
        elif not self._party_widget_seen:
            return  # never had a widget: leave it to party messages
        if members > 1:
            self._party_alone_t = None
        elif self._party_alone_t is None:
            self._party_alone_t = now
        elif now - self._party_alone_t >= PARTY_ALONE_S and self.session:
            self.session.party_widget_alone()  # kills are solo now, whatever the size label holds
        n = members or 1
        if self._party_bonus_t is not None and now - self._party_bonus_t < PARTY_SIZE_HOLD_S:
            n = max(n, 2)  # a recent party bonus line: someone is with us
        if n > self.party_size:
            self._party_low_reads = 0
            if not self._party_high_reads:
                self._party_change_t = now
            self._party_high_size = min(self._party_high_size, n) if self._party_high_reads else n
            self._party_high_reads += 1
            if self._party_high_reads >= PARTY_RAISE_READS and now - self._party_change_t >= PARTY_SETTLE_S:
                self._set_party(self._party_high_size)  # the widget agrees: more members
            return
        self._party_high_reads = 0
        if n == self.party_size:
            self._party_low_reads = 0
            return
        if not self._party_low_reads:
            self._party_change_t = now
        self._party_low_size = max(self._party_low_size, n) if self._party_low_reads else n
        self._party_low_reads += 1
        if self._party_low_reads >= PARTY_DROP_READS and now - self._party_change_t >= PARTY_SETTLE_S:
            self._set_party(self._party_low_size)  # the widget agrees: fewer members

    def on_chat_lines(self, visible_lines, now: float | None = None) -> None:
        """Chat box read: party join/leave/create messages only show there."""
        now = time.monotonic() if now is None else now
        # Rows in a known non-salmon colour (chat, whispers, notices) aren't party events.
        visible_lines = [ln for ln in visible_lines if colour_allows_party(getattr(ln, "colour", None))]
        for msg in self.chat.update(visible_lines):
            if msg.kind != "party":
                continue
            if msg.category in ("created", "disbanded") or (msg.category == "left" and msg.item == "you"):
                # Leaving beats any recent bonus line: it must not hold us in a party.
                self._party_bonus_t = None
                self._party_left_t = now
                self._set_party(1)
            elif msg.category == "joined":
                self._party_left_t = None
                self._set_party(2 if msg.item == "you" else self.party_size + 1)
            elif msg.category == "left":
                self._set_party(self.party_size - 1)

    def _left_recently(self, now: float) -> bool:
        return self._party_left_t is not None and now - self._party_left_t < PARTY_LEFT_HOLD_S

    def _set_party(self, n: int) -> None:
        self.party_size = max(1, n)
        self._party_low_reads = self._party_high_reads = 0
        if self.session:
            self.session.set_party_size(self.party_size)

    def on_message_lines(self, visible_lines, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        msgs = self.lines.update(visible_lines, t)  # always consume lines
        for msg in msgs:
            if msg.kind == "party":
                continue  # party events are read from the chat box only
            if msg.kind == "party_bonus" and not self._left_recently(t):
                self._party_bonus_t = t
                if self.party_size < 2:
                    self._set_party(2)
            if self.paused:
                continue
            if self.session is None:
                if msg.kind == "exp" and self.combat_map:
                    self._start(self._probe.last if self._probe else None, now)
                else:
                    continue
            self.session.on_message(msg, now)
        if self.session is not None and not self.paused:
            if "exp" in self.lines.shown:
                self.session.bar.shown(t)
            self.session.on_lines(msgs, now)

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
        # No closing tick: the clock was ticked by every refresh up to now (a tick on the real
        # clock here would mix with callers that pass their own ``now``).
        self.last = s
        self._retry_pending()
        path = self._save(self._record(s))
        if path is not None:  # else the autosave stays on disk as well until a retry works
            self._drop_current()
        return path

    def _dir(self) -> Path:
        return self.save_dir or sessions_dir()

    def _record(self, s: Session) -> dict:
        return {"schema_version": SCHEMA_VERSION, **s.to_dict()}

    def _notify(self, msg: str) -> None:
        for cb in self.on_notice:
            try:
                cb(msg)
            except Exception:  # noqa: BLE001 - a UI callback must never lose a session
                log.exception("notice callback failed")

    def _save(self, rec: dict, retry: bool = False) -> Path | None:
        """Write a finished session atomically, never over another file. On failure it goes to
        ``_pending`` (saved again by ``tick`` and the next ``end_session``) and the user is told once."""
        try:
            d = self._dir()
            d.mkdir(parents=True, exist_ok=True)
            path = _free_path(d, _stem(rec))
            write_atomic(path, json.dumps(rec, indent=2))
        except OSError as e:
            log.exception("session save failed")
            self._pending.append(rec)
            if not retry:
                self._notify(f"Couldn't save the session ({e.strerror or e}). It's kept and will be saved again shortly.")
            return None
        self.saved.append(path)
        if path.name.startswith(self._today.get("date", "")):
            self._add_today(rec)
        for cb in self.on_saved:
            try:
                cb(path)
            except Exception:  # noqa: BLE001 - the session is saved; a UI callback must not undo the map change
                log.exception("on_saved callback failed")
        return path

    def _retry_pending(self) -> None:
        pending, self._pending = self._pending, []
        for rec in pending:  # in order; a failure puts it back
            self._save(rec, retry=True)

    def _drop_current(self) -> None:
        with contextlib.suppress(OSError):
            (self._dir() / CURRENT_FILE).unlink(missing_ok=True)

    def autosave(self) -> None:
        """Write the running session to ``CURRENT_FILE`` so a crash, kill or power cut loses at most
        ``AUTOSAVE_S``. ``recover`` turns a leftover one into a saved session on the next start."""
        s = self.session
        if s is None or not s.has_data():
            return
        try:
            d = self._dir()
            d.mkdir(parents=True, exist_ok=True)
            write_atomic(d / CURRENT_FILE, json.dumps(self._record(s) | {"recovered": True}, indent=2))
        except Exception:  # noqa: BLE001 - runs from the UI timer: log and go on
            log.exception("autosave failed")

    def recover(self) -> str | None:
        """A session left in ``CURRENT_FILE`` (the app didn't end it: crash, kill, power cut) becomes a
        saved session. Returns a line for the user, or None. Call once at start, before the first tick."""
        d = self._dir()
        cur = d / CURRENT_FILE
        if not cur.exists():
            return None
        try:
            rec = json.loads(cur.read_text(encoding="utf-8"))
            if not isinstance(rec, dict):
                raise ValueError("not a session")
            stem = _stem(rec)
            for f in d.glob(f"{stem}*.json"):  # already saved: stopped between saving it and removing this
                try:
                    old = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(old, dict) and (old.get("started_at"), old.get("map")) == (rec.get("started_at"), rec.get("map")):
                    cur.unlink()
                    return None
            path = _free_path(d, stem)
            os.replace(cur, path)  # a rename: there are never two copies
        except (OSError, ValueError) as e:
            log.warning("couldn't recover %s: %s", cur, e)
            with contextlib.suppress(OSError):  # kept for a look, out of the way
                os.replace(cur, d / f".current.bad-{datetime.now():%Y%m%d-%H%M%S}.json")
            return None
        self.saved.append(path)
        self._load_today()
        return f"Recovered {_fmt_span(_int(rec.get('active_seconds')))} on {rec.get('map') or 'an unknown map'}"

    def tick(self, now: float | None = None) -> None:
        """Called about once a second: the session clock, and every ``AUTOSAVE_S`` the autosave."""
        now = time.monotonic() if now is None else now
        if self.session:
            self.session.tick(now)
        if self._autosave_t is None:
            self._autosave_t = now
        elif now - self._autosave_t >= AUTOSAVE_S:
            self._autosave_t = now
            self._retry_pending()
            self.autosave()

    # --- today's totals -----------------------------------------------------------

    def _load_today(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        self._today = {"date": today, "exp": 0, "kills": 0, "seconds": 0, "sessions": 0,
                       "mesos": 0, "mesos_spent": 0}
        d = self._dir()
        try:
            files = sorted(d.glob(f"{today}-*.json")) if d.exists() else []
        except OSError:
            log.exception("couldn't list %s", d)
            files = []
        for f in files:
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(rec, dict):
                    raise ValueError("not a session")
                self._add_today(rec)
            except Exception as e:  # noqa: BLE001 - one odd file must never stop the app starting
                log.warning("today's totals: skipped %s: %s", f.name, e)

    def _add_today(self, rec: dict) -> None:
        for k, src in (("exp", "exp_gained"), ("kills", "party_kills"), ("seconds", "active_seconds"),
                       ("mesos", "mesos"), ("mesos_spent", "mesos_spent")):
            self._today[k] += _int(rec.get(src))
        self._today["sessions"] += 1

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

    def _start(self, baseline: Reading | None, now: float | None) -> None:
        if self._probe is not None:  # what the probe learned while waiting
            self.learned_exp = dict(self._probe.learned)
            self.distrusted = set(self._probe.distrusted)
            self._probe = None
        s = Session(map_name=self.map_name, mobs=self.mobs, item_resolver=self.item_resolver,
                    prices=self.prices, slot_items=self.slot_items,
                    party_size=self.party_size)
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
            s._note_start()
        self.session = s
