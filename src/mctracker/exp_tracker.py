"""EXP tracking from periodic (time, level, exp, percent) readings of the bar.

Readings come from screen recognition, so any field can be misread. The
tracker solves for the level instead of trusting the digits: the bar shows
"exp [pct%]" with pct = round_half_up(exp / need(L) * 100, 2), and with the EXP
table usually only one level fits a given (exp, pct) pair. A reading that fits
exactly one level is *validated*. A position is sum(need(1..L-1)) + exp.

Governing principle
-------------------
Book only plausible movement. Anything implausible either waits for strong
confirmation or re-anchors WITHOUT booking. Error may only ever undercount,
never inflate, and tracking must never freeze.

1. Plausible step. A move from the committed reading is plausible when it is
   a same-level rise (or no change) or a +1 level-up, and its size is at most
   ``step_cap(L)`` = max(STEP_FRACTION of need(L), STEP_RATE_FACTOR x the best
   per-reading gain of the last RATE_WINDOW_S, a low-level floor). Plausible
   steps book at once. After the bar went unseen for a while (a capture gap,
   or readings that fit no level), the rate term is scaled by the number of
   typical reading intervals it missed, up to one level; readings that were
   seen but rejected never widen it.
2. Implausible rises (multi-level jumps, rises above the cap: quests, combined
   misreads) are never booked. Such a reading starts a candidate chain; once
   CANDIDATE_CONFIRM later readings with distinct raw EXP are each a plausible
   step from the chain, with the digits agreeing with their fitted level, the
   tracker re-anchors there without booking the jump. **Quest EXP and other
   big non-grind jumps are therefore never counted**: this tracks grinding.
   A re-anchor *below* the committed position takes back what was booked
   above the new position since the segment's anchor (all of it, if the new
   position is below the anchor): see ``retracted``.
3. Never freeze. Any CONTRADICT_CONFIRM readings with distinct raw EXP that
   are not plausible from the committed reading, but are plausible steps among
   themselves, re-anchor the tracker without booking, whatever the digits say
   (a level whose digit crop misreads throughout, a stale or misread anchor).
   Steps inside a chain may be up to CHAIN_STEP_FACTOR x the cap, and a
   re-anchor seeds the rate term with the chain's biggest step, so grinding
   faster than the cap from the first kill still recovers.
4. Level digits only break ties between fits, and agree with candidates. They
   never block a plausible level-up. Unreadable digits arrive as ``None``.
5. Deaths. A same-level drop from need // 100 (less one percent step) to
   DEATH_MAX_FRACTION of the level, or down to exactly 0, is a loss once
   DEATH_CONFIRM readings with distinct raw EXP show it (identical repeats
   are no evidence; any later changed frame below the pre-death position
   confirms it). At session end a pending dip is booked. A booked death is
   never taken back.
6. Spikes. A plausible step can still be a misread inside the cap. Until
   SPIKE_WINDOW validated readings with other raw EXP have followed it
   (unreadable or unfitting frames don't count), it is young: a reading that
   lands back near where it started takes it back, and any drop is judged
   from where it started. A drop that is a plausible step or a death from
   there takes the young step back first. So the last kill before a death is
   not counted (net is still right, gained and lost are one kill lower).

Interface used by the kill counter (PR #9)
------------------------------------------
``verified_gain``
    Booked plausible movement (net change plus confirmed deaths), excluding
    the last step while it's inside its spike window and what ``finalize``
    books from an unreadable-percent tail. Never goes down; deaths are
    reported separately in ``lost``.
``unconfirmed_gain``
    The booked step still inside its spike window. On a still bar a fresh
    kill stays here until the frame changes twice, so a kill cap should use
    ``verified_gain + unconfirmed_gain``.
``retracted``
    Gain taken back by downward re-anchors. ``verified_gain`` never goes down,
    so it can still include some of it; a kill cap that must never exceed
    booked EXP should use ``verified_gain + unconfirmed_gain - retracted``
    and, when that falls, stop counting new kills rather than uncount old ones.
``behind``
    True while something is pending: a rejected reading since the last
    commit, an ambiguous reading, a candidate or contradiction, a death dip,
    a level being learned, or a start not anchored yet.
``current_gain``
    ``verified_gain + unconfirmed_gain - retracted`` without the high-water
    mark: it falls when a misread step is taken back. Party kills count bar
    steps from it (a step taken back is no kill).
``last_committed_t``
    When the last reading was committed, in the clock of the ``now`` passed to
    ``add``/``seed``. ``Session`` passes ``time.monotonic()``.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Mapping

from .exp_table import EXP_TABLE, bar_percent

log = logging.getLogger(__name__)

# A shown percent fits a level when it's within this of the predicted value.
PCT_TOLERANCE = 0.01
# Rule 1: the largest step booked straight away is the biggest of these.
STEP_FRACTION = 0.10  # of the level
STEP_RATE_FACTOR = 3  # x the best per-reading gain over the last RATE_WINDOW_S
RATE_WINDOW_S = 300.0
# The typical reading interval is the median of the last INTERVAL_KEEP gaps
# between readings, once there are INTERVAL_MIN of them.
INTERVAL_KEEP = 30
INTERVAL_MIN = 3
# Low levels: a kill can be most of a level. Up to LOW_LEVEL_FULL any step up
# to a whole level is plausible; above it the floor shrinks to STEP_FRACTION
# by LOW_LEVEL_END.
LOW_LEVEL_FULL = 25
LOW_LEVEL_END = 40
# Rule 2/3: later readings with distinct raw EXP needed to re-anchor.
CANDIDATE_CONFIRM = 3  # after an implausible rise, digits agreeing
CONTRADICT_CONFIRM = 5  # any contradiction, digits ignored
MAX_CHAINS = 8
# Steps inside a chain may be up to this x the cap: grinding faster than the
# cap before anything was booked must still be able to confirm a chain. On a
# re-anchor the chain's biggest step seeds the rate term (its jump never does).
CHAIN_STEP_FACTOR = 2
# Rule 5: deaths.
DEATH_MAX_FRACTION = 0.10
DEATH_MIN_FRACTION = 0.01  # town death; less one percent step (need // 100 < need/100)
DEATH_CONFIRM = 2
# Rule 6: spikes are taken back within SPIKE_WINDOW changed frames when the
# reading lands within SPIKE_LANDING of the step above where it started.
SPIKE_WINDOW = 2
SPIKE_LANDING = 0.25
# At the start, readings whose fit disagrees with the digits before one is used.
START_CONFIRM = 5
# With the percent unreadable, a same-level gain is booked at session end only
# if the last this-many readings all show it.
UNVALIDATED_HOLD = 3
# Learning a level's size from the bar: intersect the need intervals of many
# readings and accept once the interval is this narrow.
LEARN_MIN_READINGS = 10
LEARN_MIN_DISTINCT = 3
LEARN_MAX_WIDTH = 0.002
# Overruling a table entry needs much more: readings with exactly that level
# in the digits, at the committed level or the next one.
DISTRUST_MIN_READINGS = 30
DISTRUST_MIN_DISTINCT = 5
# Readings that fit a distrusted entry's table size again before it's restored.
TRUST_BACK_READINGS = 10


@dataclass(frozen=True)
class Reading:
    t: float  # seconds (monotonic)
    level: int | None  # level digits as read (a tie-breaker); None if unreadable
    exp: int  # EXP into the current level
    pct: float | None = None  # bar percent, None when unreadable


@dataclass
class _Learner:
    lo: float
    hi: float
    n: int = 1
    exps: set[int] = field(default_factory=set)


@dataclass
class _Dip:
    ref: Reading  # committed reading before the drop
    low: int  # lowest EXP seen since
    exps: set[int]  # distinct raw EXP seen in the dip: identical repeats aren't evidence


@dataclass
class _Chain:
    """Readings not plausible from the committed one, but plausible among themselves."""

    first: Reading
    last: Reading
    exps: set[int]  # distinct raw EXP values
    rise: bool  # starts above the committed position (a candidate, rule 2)
    digits_ok: bool  # every reading's digits said its fitted level
    best: int = 0  # biggest step between chain readings


def _related(digits: int | None, level: int) -> bool:
    """Could ``digits`` be a misread of ``level`` (a dropped, extra or off-by-one digit)?"""
    return digits in {level, level - 1, level + 1, level - 10, level + 10, level // 10, level * 10}


@dataclass
class ExpTracker:
    # EXP needed to go from level L to L+1 (trusted, e.g. the shipped table).
    exp_table: Mapping[int, int] = field(default_factory=lambda: EXP_TABLE)
    window_s: float = 600.0
    # Sizes learned from the bar for levels the table lacks or got wrong.
    learned: dict[int, int] = field(default_factory=dict)
    distrusted: set[int] = field(default_factory=set)

    lost: int = field(default=0, init=False)  # confirmed death losses
    rejected_total: int = field(default=0, init=False)  # readings never committed
    reanchors: int = field(default=0, init=False)  # rule 2/3 re-anchors (nothing booked)
    # Gain taken back by downward re-anchors (it may already be in verified_gain).
    retracted: int = field(default=0, init=False)
    warnings: list[str] = field(default_factory=list, init=False)
    # Set by add() when a reading corrects an earlier step: the part of the
    # returned delta that takes that step back (the rest is new gain).
    undone: int = field(default=0, init=False)

    _cur: Reading | None = field(default=None, init=False)  # committed reading
    _prev: Reading | None = field(default=None, init=False)  # committed before it
    _anchor: Reading | None = field(default=None, init=False)  # segment start
    _seg: int = field(default=0, init=False)  # gain from anchor to _cur
    _step: int = field(default=0, init=False)  # last rise (0 once it was taken back)
    _step_raw: int | None = field(default=None, init=False)  # raw EXP of that rise
    _step_frames: set[int] = field(default_factory=set, init=False)  # other raw EXP seen since
    _banked: int = field(default=0, init=False)  # gain of closed segments
    _levels_banked: int = field(default=0, init=False)
    _tail_booked: int = field(default=0, init=False)  # booked from unreadable percent
    _commits: int = field(default=0, init=False)
    _started: bool = field(default=False, init=False)
    _start_t: float | None = field(default=None, init=False)
    _start_candidates: list[Reading] = field(default_factory=list, init=False)
    _start_disagree: list[Reading] = field(default_factory=list, init=False)
    # Readings not committed since the last commit: raw, and ambiguous with fits.
    _tail: list[Reading] = field(default_factory=list, init=False)
    _ambiguous: list[tuple[Reading, list[int]]] = field(default_factory=list, init=False)
    _dip: _Dip | None = field(default=None, init=False)
    _chains: list[_Chain] = field(default_factory=list, init=False)
    _now: float | None = field(default=None, init=False)  # caller's clock of the reading
    _committed_at: float | None = field(default=None, init=False)
    _verified_hw: int = field(default=0, init=False)  # verified_gain never goes down
    _learning: bool = field(default=False, init=False)  # last reading fed a learner
    _learners: dict[int, _Learner] = field(default_factory=dict, init=False)
    _trust_back: dict[int, int] = field(default_factory=dict, init=False)
    # (t, cumulative net EXP) for the rolling window
    _history: deque[tuple[float, int]] = field(default_factory=deque, init=False)
    # (t, gain) of booked rises, for the rate cap
    _gains: deque[tuple[float, int]] = field(default_factory=deque, init=False)
    _last_t: float | None = field(default=None, init=False)  # last reading of any kind
    _seen_t: float | None = field(default=None, init=False)  # last reading that fitted a level, before this one
    _fit_now: bool = field(default=False, init=False)
    _intervals: deque[float] = field(default_factory=lambda: deque(maxlen=INTERVAL_KEEP), init=False)

    # --- results -----------------------------------------------------------------

    @property
    def net(self) -> int:
        return self._banked + self._seg

    @property
    def gained(self) -> int:
        return self.net + self.lost

    @property
    def verified_gain(self) -> int:
        """Booked plausible movement plus deaths, minus a step still inside its
        spike window (see module doc). Never goes down."""
        self._verified_hw = max(self._verified_hw, self.gained - self._tail_booked - self._unconfirmed_step())
        return self._verified_hw

    def _unconfirmed_step(self) -> int:
        """The last rise, until SPIKE_WINDOW validated readings with other raw
        EXP have followed it (unreadable or unfitting frames don't count)."""
        return self._step if self._step > 0 and len(self._step_frames) < SPIKE_WINDOW else 0

    @property
    def unconfirmed_gain(self) -> int:
        return self._unconfirmed_step()

    @property
    def current_gain(self) -> int:
        """``verified_gain + unconfirmed_gain - retracted`` as the bar stands
        now: it falls when a misread step is taken back (``verified_gain``
        never does, so a spike verified by a second spike stays in it). Never
        above that sum."""
        return self.gained - self._tail_booked - self.retracted

    @property
    def behind(self) -> bool:
        """True while a reading is pending that may still move the position."""
        return bool(
            self._ambiguous
            or self._chains
            or self._dip is not None
            or self._learning
            or self._start_disagree
            or self._tail
        )

    @property
    def last_committed_t(self) -> float | None:
        """When the last reading was committed, in the clock of the ``now``
        passed to ``add``/``seed`` (``Reading.t`` when none was passed)."""
        return self._committed_at

    @property
    def levels_gained(self) -> int:
        if self._cur is None or self._anchor is None:
            return self._levels_banked
        return self._levels_banked + self._cur.level - self._anchor.level

    @property
    def last(self) -> Reading | None:
        """The committed (validated) reading; its ``level`` is the solved level."""
        return self._cur

    @property
    def previous(self) -> Reading | None:
        """The committed reading before ``last``."""
        return self._prev

    @property
    def level(self) -> int | None:
        """The committed level, None before the first validated reading."""
        return self._cur.level if self._cur is not None else None

    # --- table -------------------------------------------------------------------

    def need(self, level: int) -> int | None:
        """Size of ``level``: the table's, unless it was found wrong, else learned."""
        if self._trusted(level):
            return self.exp_table[level]
        return self.learned.get(level)

    def _trusted(self, level: int) -> bool:
        return level in self.exp_table and level not in self.distrusted

    def _slack(self, level: int) -> float:
        """One displayed percent step of ``level``, in EXP."""
        need = self.need(level) or 0
        return max(1.0, need * PCT_TOLERANCE / 100)

    def fits(self, r: Reading) -> list[int]:
        """Levels whose size explains the reading's (exp, pct)."""
        if r.pct is None:
            return []
        out = []
        for lvl in sorted({*self.exp_table, *self.learned}):
            need = self.need(lvl)
            if (
                need
                and 0 <= r.exp < need
                and abs(r.exp * 100 / need - r.pct) <= PCT_TOLERANCE + 0.01  # cheap float filter first
                and abs(bar_percent(r.exp, need) - r.pct) <= PCT_TOLERANCE + 1e-9
            ):
                out.append(lvl)
        return out

    def _dist(self, a: Reading, b: Reading) -> int | None:
        """EXP from ``a`` to ``b``; None if a level in between has no known size."""
        if a.level == b.level:
            return b.exp - a.exp
        lo, hi = sorted((a.level, b.level))
        sizes = [self.need(lvl) for lvl in range(lo, hi)]
        if any(s is None for s in sizes):
            return None
        span = sum(sizes)  # type: ignore[arg-type]
        if b.level > a.level:
            return span - a.exp + b.exp
        return -(span - b.exp + a.exp)

    # --- plausibility --------------------------------------------------------------

    def step_cap(self, level: int, t: float | None = None, dt: float | None = None) -> float:
        """Rule 1: the largest step from ``level`` booked straight away, ``dt``
        seconds after the committed reading. The rate term grows with the
        number of typical reading intervals in ``dt`` (a capture gap), but
        that growth stops at one level."""
        need = self.need(level)
        if not need:
            return 0.0
        frac = STEP_FRACTION
        if level <= LOW_LEVEL_FULL:
            frac = 1.0
        elif level < LOW_LEVEL_END:
            frac += (1.0 - STEP_FRACTION) * (LOW_LEVEL_END - level) / (LOW_LEVEL_END - LOW_LEVEL_FULL)
        rate = STEP_RATE_FACTOR * self._best_recent_gain(t)
        typical = self._typical_interval()
        if dt is not None and typical and dt > typical:
            rate = min(rate * dt / typical, max(rate, need))
        return max(need * frac, rate)

    def _unseen(self, a: Reading, b: Reading) -> float:
        """How long the bar went unseen before ``b``: since the last reading
        that fitted a level (rejected or not), at most since ``a``. Readings
        that were seen but rejected (a quest reward read again and again)
        never widen the cap; a capture gap or unreadable percent does."""
        since = a.t if self._seen_t is None else max(a.t, self._seen_t)
        return b.t - since

    def _typical_interval(self) -> float | None:
        """Median time between readings, once there are a few."""
        if len(self._intervals) < INTERVAL_MIN:
            return None
        xs = sorted(self._intervals)
        mid = xs[len(xs) // 2]
        return mid if mid > 0 else None

    def _best_recent_gain(self, t: float | None) -> int:
        """Best confirmed per-reading gain of the last RATE_WINDOW_S."""
        g = self._gains
        if t is not None:
            while g and g[0][0] < t - RATE_WINDOW_S:
                g.popleft()
        young = self._unconfirmed_step() > 0
        vals = list(g)[:-1] if young else list(g)  # a step that may be taken back never counts
        return max((v for _, v in vals), default=0)

    def _plausible(self, a: Reading, b: Reading, d: int | None = None, factor: float = 1) -> bool:
        """Rule 1: a same-level rise (or no change) or a +1, within the cap."""
        if d is None:
            d = self._dist(a, b)
        if d is None or d < 0 or b.level - a.level not in (0, 1):
            return False
        return d <= factor * self.step_cap(a.level, b.t, self._unseen(a, b))

    def _acceptable(self, f: Reading) -> bool:
        """Would ``f`` move the committed reading without waiting (rule 1, 5 or 6)?"""
        cur = self._cur
        assert cur is not None
        d = self._dist(cur, f)
        if d is None:
            return False
        if d >= 0:
            return self._plausible(cur, f, d)
        return self._is_correction(f) or self._death_like(f, -d)

    # --- input -------------------------------------------------------------------

    def seed(self, r: Reading, now: float | None = None) -> None:
        """Start from a reading someone else already validated (no gain booked)."""
        self._started = True
        self._anchor = self._cur = r
        self._prev = None
        self._seg = self._step = 0
        self._committed_at = r.t if now is None else now
        self._clear_pending()

    def add(self, r: Reading, now: float | None = None) -> int:
        """Record a reading; returns the change in session gain it causes.
        ``now`` is the caller's clock for ``last_committed_t`` (default ``r.t``)."""
        if self._start_t is None:
            self._start_t = r.t
        self._now = r.t if now is None else now
        self.undone = 0
        if self._last_t is not None:
            self._intervals.append(r.t - self._last_t)
        self._last_t = r.t
        self._fit_now = False
        self._learning = False
        self._check_trust_back(r)
        before = self._commits
        delta = self._add(r)
        if self._fit_now:
            self._seen_t = r.t
        if self._commits == before:
            self.rejected_total += 1
            self._tail.append(r)
        self._record(r.t)
        return delta

    def _add(self, r: Reading) -> int:
        if r.pct is None:
            return 0
        levels = self.fits(r)
        self._fit_now = bool(levels)
        if not levels:
            self._learn(r)
            return 0
        if len(levels) > 1:
            return self._ambiguous_reading(r, levels)
        fit = Reading(r.t, levels[0], r.exp, r.pct)
        if not self._crossing_ok(fit, r):
            return 0
        if not self._trusted(fit.level):
            self._learn(fit, validated=True)  # keep narrowing a learned size
        return self._validated(fit, r)

    def _crossing_ok(self, fit: Reading, raw: Reading) -> bool:
        # A crossing into a level only a learned size explains needs digits
        # that could be that level (and aren't the committed one).
        cur = self._cur
        if cur is None or fit.level == cur.level or self._trusted(fit.level):
            return True
        return raw.level != cur.level and _related(raw.level, fit.level)

    def _ambiguous_reading(self, r: Reading, levels: list[int]) -> int:
        """Several levels fit (EXP near 0%). A unique acceptable move wins, then
        the digits; otherwise it waits for a reading that fits one level, or
        for ``finalize``."""
        cur = self._cur
        if cur is not None:
            ok = [lvl for lvl in levels if self._acceptable(Reading(r.t, lvl, r.exp, r.pct))]
            pick = ok[0] if len(ok) == 1 else (r.level if r.level in ok else None)
            if pick is not None:
                return self._validated(Reading(r.t, pick, r.exp, r.pct), r)
        elif r.level in levels:
            return self._validated(Reading(r.t, r.level, r.exp, r.pct), r)
        self._ambiguous.append((r, levels))
        if cur is None and not self._started:
            self._start_candidates = [Reading(r.t, lvl, r.exp, r.pct) for lvl in levels]
        return 0

    def _validated(self, fit: Reading, raw: Reading) -> int:
        if self._step_raw is not None and raw.exp != self._step_raw:
            self._step_frames.add(raw.exp)  # only a validated changed frame ages the last rise
        cur = self._cur
        if cur is None:
            return self._begin(fit, raw)
        d = self._dist(cur, fit)
        if self._dip is not None:
            res = self._dip_reading(fit, raw)
            if res is not None:
                return res
        if d is None:
            if fit.level == cur.level + 1 and fit.exp <= self.step_cap(cur.level, fit.t, self._unseen(cur, fit)):
                return self._bank_crossing(fit)  # past a level of unknown size
            return self._contradicted(fit, raw)
        if d >= 0 and self._plausible(cur, fit, d):
            self._chains = []
            return self._commit(fit, d)
        if d < 0:
            if self._is_correction(fit):  # also back across a misread level-up
                self.undone = self._step
                self._drop_young_gain()
                return self._commit(fit, d)
            res = self._drop_after_young_step(fit, raw)
            if res is not None:
                return res
            if self._death_like(fit, -d):
                self._dip = _Dip(cur, fit.exp, {raw.exp})
                return 0
        return self._contradicted(fit, raw)

    def _contradicted(self, fit: Reading, raw: Reading) -> int:
        """Rules 2 and 3: collect readings the committed one can't explain into
        chains of plausible steps; re-anchor (booking nothing) once one is long enough."""
        cur = self._cur
        assert cur is not None
        digits_ok = raw.level == fit.level
        extended = False
        for c in self._chains:
            step = self._dist(c.last, fit)
            if self._plausible(c.last, fit, step, CHAIN_STEP_FACTOR):
                c.best = max(c.best, step or 0)
                c.last = fit
                c.exps.add(raw.exp)  # identical repeats add nothing
                c.digits_ok = c.digits_ok and digits_ok
                extended = True
        if not extended:
            d = self._dist(cur, fit)
            rise = fit.level > cur.level or (d is not None and d > 0)
            self._chains.append(_Chain(fit, fit, {raw.exp}, rise, digits_ok))
            del self._chains[:-MAX_CHAINS]
        for c in self._chains:
            later = len(c.exps - {c.first.exp})
            if (c.rise and c.digits_ok and later >= CANDIDATE_CONFIRM) or len(c.exps) >= CONTRADICT_CONFIRM:
                return self._reanchor(c)
        return 0

    def _reanchor(self, c: _Chain) -> int:
        cur = self._cur
        assert cur is not None
        msg = (
            f"EXP bar moved from L{cur.level} {cur.exp:,} to L{c.last.level} {c.last.exp:,} "
            f"implausibly and stayed; re-anchoring without booking it"
        )
        log.warning(msg)
        self.reanchors += 1
        self._rebase(c.last)
        if c.best > 0:
            self._gains.append((c.last.t, c.best))  # how fast the bar really moves
        self._commits += 1
        self._committed_at = self._now
        return 0

    def _is_correction(self, fit: Reading) -> bool:
        """Does ``fit`` land back where the last rise started (that rise a misread)?"""
        if self._step <= 0 or self._prev is None or len(self._step_frames) > SPIKE_WINDOW:
            return False
        back = self._dist(self._prev, fit)
        return back is not None and -self._slack(fit.level) <= back <= self._step * SPIKE_LANDING

    def _drop_after_young_step(self, fit: Reading, raw: Reading) -> int | None:
        """A drop while the last rise is still young is judged from where that
        rise started (``_prev``), not from the rise, which may be the misread.
        If the reading is a plausible step from there, or a death-sized drop
        (from there or from the rise), the rise is taken back and the reading
        handled from its base: a death's loss is counted from the base."""
        base, anchor = self._prev, self._anchor
        if self._step <= 0 or base is None or anchor is None or len(self._step_frames) > SPIKE_WINDOW:
            return None
        start = self._dist(anchor, base)
        d0 = self._dist(base, fit)
        if start is None or start < 0 or d0 is None:
            return None  # the base is outside this segment (a banked crossing)
        if d0 >= 0:
            if not self._plausible(base, fit, d0):
                return None
        else:
            cur = self._cur
            assert cur is not None
            if not (self._death_like(fit, -d0, base) or self._death_like(fit, cur.exp - fit.exp)):
                return None
        step = self.undone = self._step
        self._drop_young_gain()
        self._seg -= step
        self._cur, self._prev = base, None
        self._step, self._step_raw, self._step_frames = 0, None, set()
        if d0 >= 0:
            return self._validated(fit, raw) - step
        self._dip = _Dip(base, fit.exp, {raw.exp})
        return -step

    def _drop_young_gain(self) -> None:
        if self._gains and self._gains[-1][1] == self._step:
            self._gains.pop()  # a taken-back step never raises the cap

    def _death_like(self, fit: Reading, drop: int, ref: Reading | None = None) -> bool:
        """Is a same-level drop of ``drop`` from ``ref`` (default: the committed
        reading) the size of a death penalty: 1-10% of the level, or to 0?"""
        ref = ref or self._cur
        need = self.need(fit.level)
        if ref is None or fit.level != ref.level or not need or drop <= 0:
            return False
        slack = self._slack(fit.level)
        if drop > need * DEATH_MAX_FRACTION + slack:
            return False
        # A town death costs need // 100, a bit under need * 1% when need isn't
        # a multiple of 100: allow one percent step below.
        return drop >= need * DEATH_MIN_FRACTION - slack or fit.exp == 0

    def _dip_reading(self, fit: Reading, raw: Reading) -> int | None:
        dip = self._dip
        assert dip is not None
        ref = dip.ref
        if fit.level != ref.level or fit.exp >= ref.exp:
            self._dip = None  # back up: the drop was a misread
            return None
        if raw.exp in dip.exps:
            return 0  # an identical repeat is no evidence
        if len(dip.exps) + 1 < DEATH_CONFIRM:
            dip.exps.add(raw.exp)
            dip.low = min(dip.low, fit.exp)
            return 0
        # A changed frame below the pre-death position confirms the death down
        # to the dip's low; the frame itself is then judged from there (a kill
        # after respawning, or a further drop).
        delta = self._book_dip(Reading(fit.t, ref.level, dip.low, None))
        if fit.exp == dip.low:
            return delta
        return delta + self._validated(fit, raw)

    def _book_dip(self, at: Reading) -> int:
        dip = self._dip
        assert dip is not None
        ref = dip.ref
        loss = ref.exp - dip.low
        self.lost += loss
        self._dip = None
        log.info("death at level %d: -%d EXP", at.level, loss)
        return self._commit(at, at.exp - ref.exp) + loss

    def _begin(self, fit: Reading, raw: Reading) -> int:
        """First validated reading of a segment (session start, or after a pause)."""
        start = fit
        if not self._started:
            if raw.level != fit.level:  # wait for a reading the digits agree with
                self._start_disagree.append(fit)
                same = [f for f in self._start_disagree if f.level == fit.level]
                if len({f.exp for f in same}) < START_CONFIRM:  # changed frames only
                    return 0
                d = self._dist(same[0], fit)
                start = same[0] if d is not None and d >= 0 else fit
            best = None
            for c in self._start_candidates:  # earlier 0.00% readings
                d = self._dist(c, start)
                if d is not None and d >= 0 and (best is None or d < best[0]):
                    best = (d, c)  # lowest-gain explanation
            if best is not None:
                start = best[1]
            self._started = True
            self._start_candidates = []
            self._start_disagree = []
        self._anchor = self._cur = start
        self._prev = None
        self._seg = self._step = 0
        self._commits += 1
        self._committed_at = self._now
        self._clear_pending()
        if start is fit:
            return 0
        return self._commit(fit, self._dist(start, fit) or 0)

    def _rebase(self, r: Reading) -> None:
        """Close the segment and start a new one at ``r``. Moving up keeps the
        segment's gain. Moving down keeps only the gain from the anchor to
        ``r`` (none if ``r`` is below the anchor or the distance is unknown):
        what was booked above ``r`` is taken back into ``retracted``."""
        cur, anchor = self._cur, self._anchor
        if cur is not None and anchor is not None:
            seg, levels = self._seg, cur.level - anchor.level
            d = self._dist(cur, r)
            if (d < 0) if d is not None else r.level < cur.level:
                back = self._dist(anchor, r)
                keep = min(seg, max(back or 0, 0))
                levels = min(levels, r.level - anchor.level) if back is not None and back >= 0 else min(levels, 0)
                self.retracted += seg - keep
                if seg > keep:
                    log.warning("re-anchored below the booked position: %d EXP taken back", seg - keep)
                seg = keep
            self._banked += seg
            self._levels_banked += levels
        self._anchor = self._cur = r
        self._prev = None
        self._seg = self._step = 0
        self._step_raw = None
        self._clear_pending()

    def _bank_crossing(self, r: Reading) -> int:
        """A level-up past a level whose size isn't known: keep the EXP made
        so far and the new level's EXP, but not the unknown remainder."""
        assert self._cur is not None and self._anchor is not None
        log.info("level %d -> %d crossed an unknown level size; remainder not booked", self._cur.level, r.level)
        self._banked += self._seg
        self._levels_banked += r.level - self._anchor.level
        self._anchor = Reading(r.t, r.level, 0, 0.0)
        self._seg = 0
        return self._commit(r, r.exp)

    def _commit(self, r: Reading, delta: int) -> int:
        cur = self._cur
        if delta == 0 and cur is not None and (cur.level, cur.exp) == (r.level, r.exp):
            self._cur = r  # a repeat never becomes the spike base (_prev)
        else:
            self._prev, self._cur = cur, r
        self._seg += delta
        if delta > 0:
            self._step, self._step_raw, self._step_frames = delta, r.exp, set()
            self._gains.append((r.t, delta))
        elif delta < 0:
            self._step, self._step_raw, self._step_frames = 0, None, set()
        self._commits += 1
        self._committed_at = self._now
        self._clear_pending()
        return delta

    def _clear_pending(self) -> None:
        self._tail = []
        self._ambiguous = []
        self._dip = None
        self._chains = []

    def _record(self, t: float) -> None:
        self._history.append((t, self.net))
        while len(self._history) > 2 and self._history[0][0] < t - self.window_s:
            self._history.popleft()

    # --- learning ------------------------------------------------------------------

    def _learn_target(self, r: Reading) -> int | None:
        """The level a reading no known size explains could teach us about:
        the committed level or the next one, if the table lacks it (and the
        digits could be it), or exactly the digits' level (to overrule the
        table, which needs much more evidence)."""
        cur = self._cur
        if cur is None:
            return None
        near = (cur.level + 1, cur.level)  # the next level first: a level-up is likelier
        if r.level in near:  # the digits name one of them exactly
            return r.level
        for lvl in near:
            if lvl not in self.exp_table and _related(r.level, lvl):
                return lvl
        return None

    def _learn(self, r: Reading, validated: bool = False) -> None:
        """Narrow down a level's size from a reading's (exp, pct)."""
        if r.pct is None or r.pct <= 0:
            return
        lvl = r.level if validated else self._learn_target(r)
        if lvl is None or lvl <= 0:
            return
        if not validated and lvl not in self.learned:
            self._learning = True
        lo = max(r.exp + 1, r.exp * 100 / (r.pct + 0.005))
        hi = r.exp * 100 / (r.pct - 0.005) if r.pct > 0.005 else float("inf")
        ln = self._learners.get(lvl)
        if ln is None or max(ln.lo, lo) > min(ln.hi, hi):
            ln = self._learners[lvl] = _Learner(lo, hi)  # (re)start: no overlap
        else:
            ln.lo, ln.hi, ln.n = max(ln.lo, lo), min(ln.hi, hi), ln.n + 1
        ln.exps.add(r.exp)
        overrule = self._trusted(lvl)
        min_n, min_distinct = (
            (DISTRUST_MIN_READINGS, DISTRUST_MIN_DISTINCT) if overrule else (LEARN_MIN_READINGS, LEARN_MIN_DISTINCT)
        )
        if ln.n < min_n or len(ln.exps) < min_distinct:
            return
        if ln.hi == float("inf") or (ln.hi - ln.lo) / ln.lo >= LEARN_MAX_WIDTH:
            return
        value = round((ln.lo + ln.hi) / 2)
        if overrule:
            table = self.exp_table[lvl]
            if ln.lo <= table <= ln.hi:
                del self._learners[lvl]
                return
            self.distrusted.add(lvl)
            self._trust_back.pop(lvl, None)
            msg = f"EXP table looks wrong for level {lvl}: the bar implies ~{value:,}, the table says {table:,}"
            self.warnings.append(msg)
            log.warning(msg)
        elif self.learned.get(lvl) != value:
            if lvl not in self.learned:
                log.info("learned EXP for level %d: ~%d", lvl, value)
            self._learning = False
        self.learned[lvl] = value

    def _check_trust_back(self, r: Reading) -> None:
        """Restore a distrusted table entry once readings fit it again."""
        lvl = r.level
        if lvl not in self.distrusted or r.pct is None:
            return
        table = self.exp_table[lvl]
        if not (0 <= r.exp < table and abs(bar_percent(r.exp, table) - r.pct) <= PCT_TOLERANCE + 1e-9):
            return
        n = self._trust_back[lvl] = self._trust_back.get(lvl, 0) + 1
        if n >= TRUST_BACK_READINGS:
            self.distrusted.discard(lvl)
            self.learned.pop(lvl, None)
            self._learners.pop(lvl, None)
            del self._trust_back[lvl]
            log.warning("EXP table entry for level %d fits the bar again; trusting it", lvl)

    # --- session end / pause ---------------------------------------------------------

    def finalize(self) -> int:
        """Book what the trailing uncommitted readings safely show (session
        ending). Returns the change in gain. A pending death dip is booked; a
        pending candidate (e.g. a quest turn-in) is not. The last rise counts
        as confirmed from here on."""
        delta = self._finalize()
        self._step = 0
        return delta

    def _finalize(self) -> int:
        cur = self._cur
        if cur is None:
            return 0
        if self._dip is not None:
            d = self._dip
            return self._book_dip(Reading(cur.t, cur.level, d.low, None))
        if self._ambiguous:
            return self._finalize_ambiguous(cur)
        tail = self._tail[-UNVALIDATED_HOLD:]
        need = self.need(cur.level)
        if (
            len(tail) == UNVALIDATED_HOLD
            and need
            and all(r.pct is None and r.level == cur.level and cur.exp < r.exp < need for r in tail)
        ):
            low = min(tail, key=lambda r: r.exp)
            if low.exp - cur.exp <= self.step_cap(cur.level, low.t, self._unseen(cur, low)):
                self._tail_booked += low.exp - cur.exp
                return self._commit(low, low.exp - cur.exp)
        return 0

    def _finalize_ambiguous(self, cur: Reading) -> int:
        amb = self._ambiguous
        up = cur.level + 1
        need = self.need(cur.level) or 0
        raw, levels = amb[-1]
        votes = sum(1 for a, lv in amb if a.level == up and up in lv)
        # A level-up needs the digits, and a bar too full for a death to have
        # emptied it; otherwise a 0.00% reading means the committed level.
        if raw.level == up and up in levels and votes * 2 > len(amb) and cur.exp >= need * DEATH_MAX_FRACTION:
            c = Reading(raw.t, up, raw.exp, raw.pct)
            d = self._dist(cur, c)
            if d is not None and d >= 0 and self._plausible(cur, c, d):
                return self._commit(c, d)
        if not all(cur.level in lv for _, lv in amb):
            return 0
        self._ambiguous = []
        delta = 0
        for a, _ in amb:  # replay them at the committed level: no change, or a death
            c = Reading(a.t, cur.level, a.exp, a.pct)
            delta += self._validated(c, c)
        if self._dip is not None:
            delta += self._book_dip(Reading(cur.t, cur.level, self._dip.low, None))
        return delta

    def reset_baseline(self) -> int:
        """Next validated reading starts a new segment (e.g. after a pause):
        EXP made in between doesn't count. Returns the change booked first."""
        delta = self.finalize()
        if self._cur is not None and self._anchor is not None:
            self._banked += self._seg
            self._levels_banked += self._cur.level - self._anchor.level
        self._seg = self._step = 0
        self._cur = self._prev = self._anchor = None
        self._clear_pending()
        return delta

    # --- rates ---------------------------------------------------------------------

    def rate_per_hour(self, now: float | None = None) -> float:
        """Net EXP/hr over the rolling window."""
        if len(self._history) < 2:
            return 0.0
        t0, e0 = self._history[0]
        t1, e1 = self._history[-1]
        end = now if now is not None else t1
        dt = end - t0
        return (e1 - e0) / dt * 3600 if dt > 0 else 0.0

    def session_rate_per_hour(self, now: float | None = None) -> float:
        if self._start_t is None or not self._history:
            return 0.0
        end = now if now is not None else self._history[-1][0]
        dt = end - self._start_t
        return self.net / dt * 3600 if dt > 0 else 0.0

    def exp_remaining(self) -> int | None:
        """EXP left to the next level, or None if the level's size isn't known."""
        if self._cur is None:
            return None
        need = self.need(self._cur.level)
        if need is None:
            return None
        left = need - self._cur.exp
        return left if left > 0 else None

    def seconds_to_level(self, now: float | None = None) -> float | None:
        """Time to the next level at the session-average rate, falling back to
        the rolling-window rate. None when there is no usable rate or size."""
        left = self.exp_remaining()
        if left is None:
            return None
        rate = self.session_rate_per_hour(now)
        if rate <= 0:
            rate = self.rate_per_hour(now)
        if rate <= 0:
            return None
        return left / rate * 3600
