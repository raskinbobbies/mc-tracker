"""EXP tracking from periodic (time, level, exp, percent) readings of the bar.

Readings come from screen recognition, so any field can be misread. The
tracker solves for the level instead of trusting the digits: the bar shows
"exp [pct%]" with pct = round_half_up(exp / need(L) * 100, 2), and with the EXP
table usually only one level fits a given (exp, pct) pair. A reading that fits
exactly one level is *validated*. When several levels fit (EXP near 0%), the
level digits break the tie. Nothing else ever moves the committed position.

Gain is derived, not accumulated: a position is sum(need(1..L-1)) + exp, and
the session's gain is the current position minus the start position, plus
confirmed death losses.

Validated readings are then checked against the committed position:

* up: committed at once. If the next one or two readings fall back to about
  where the step started, the step was a misread and is taken back (a spike
  correction). A step that held for three readings is real.
* down, same level, 1-10% of the level (or down to exactly 0): a possible
  death. It's booked once DEATH_CONFIRM readings in a row show it.
* any other drop (bigger than a death, a lower level, a sub-death flicker):
  held as *suspect* and never committed straight away. A reading back at or
  above the committed position drops it. If SUSPECT_CONFIRM consistent readings
  show it, the tracker re-anchors there without booking a loss (or, when it
  undoes the last step, takes that step back).

Interface used by the kill counter (PR #9)
------------------------------------------
``verified_gain``
    Committed position change plus confirmed deaths. It never includes
    unvalidated readings (unreadable percent, no fit, ambiguous) or suspects,
    and leaves out what ``finalize`` books from an unreadable-percent tail.
``behind``
    True while the tracker knows the bar moved but hasn't committed it yet: a
    pending ambiguous reading, a suspect drop, an unconfirmed death dip, a
    level being learned, or a start not anchored yet. EXP the messages show
    may legitimately be ahead of ``verified_gain`` then.
``last_committed_t``
    Timestamp (the ``t`` passed in) of the last committed reading, or None.
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
# A same-level drop is a death if it's DEATH_MIN..DEATH_MAX of the level (or
# lands at exactly 0) and holds for DEATH_CONFIRM readings in a row.
DEATH_MAX_FRACTION = 0.10
DEATH_MIN_FRACTION = 0.01
DEATH_CONFIRM = 3
# A rise is a misread if one of the next SPIKE_WINDOW readings lands back
# within SPIKE_LANDING of the step above where it started.
SPIKE_WINDOW = 2
SPIKE_LANDING = 0.25
# Readings a suspect drop must hold for before the tracker follows it.
SUSPECT_CONFIRM = 5
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
    level: int  # level digits as read (only a tie-breaker)
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
    n: int = 1


@dataclass
class _Suspect:
    first: Reading
    last: Reading
    n: int = 1


def _related(digits: int, level: int) -> bool:
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
    warnings: list[str] = field(default_factory=list, init=False)
    # Set by add() when a reading corrects an earlier step: the part of the
    # returned delta that takes that step back (the rest is new gain).
    undone: int = field(default=0, init=False)

    _cur: Reading | None = field(default=None, init=False)  # committed reading
    _prev: Reading | None = field(default=None, init=False)  # committed before it
    _anchor: Reading | None = field(default=None, init=False)  # segment start
    _seg: int = field(default=0, init=False)  # gain from anchor to _cur
    _step: int = field(default=0, init=False)  # last rise (0 once it can't be a spike)
    _age: int = field(default=0, init=False)  # readings since that rise
    _banked: int = field(default=0, init=False)  # gain of closed segments
    _levels_banked: int = field(default=0, init=False)
    _tail_booked: int = field(default=0, init=False)  # booked from unreadable percent
    _commits: int = field(default=0, init=False)
    _anchor_n: int = field(default=0, init=False)  # validated readings at the anchor's level
    _started: bool = field(default=False, init=False)
    _start_t: float | None = field(default=None, init=False)
    _start_candidates: list[Reading] = field(default_factory=list, init=False)
    _start_disagree: list[Reading] = field(default_factory=list, init=False)
    # Readings not committed since the last commit: raw, and ambiguous with fits.
    _tail: list[Reading] = field(default_factory=list, init=False)
    _ambiguous: list[tuple[Reading, list[int]]] = field(default_factory=list, init=False)
    _dip: _Dip | None = field(default=None, init=False)
    _suspect: _Suspect | None = field(default=None, init=False)
    _learning: bool = field(default=False, init=False)  # last reading fed a learner
    _learners: dict[int, _Learner] = field(default_factory=dict, init=False)
    _trust_back: dict[int, int] = field(default_factory=dict, init=False)
    # (t, cumulative net EXP) for the rolling window
    _history: deque[tuple[float, int]] = field(default_factory=deque, init=False)

    # --- results -----------------------------------------------------------------

    @property
    def net(self) -> int:
        return self._banked + self._seg

    @property
    def gained(self) -> int:
        return self.net + self.lost

    @property
    def verified_gain(self) -> int:
        """Committed position change plus confirmed deaths (see module doc)."""
        return self.gained - self._tail_booked

    @property
    def behind(self) -> bool:
        """True while a reading is pending that may still move the position."""
        return bool(
            self._ambiguous
            or self._suspect is not None
            or self._dip is not None
            or self._learning
            or self._start_disagree
            or (self._cur is None and self._tail)
        )

    @property
    def last_committed_t(self) -> float | None:
        return self._cur.t if self._cur is not None else None

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
            if need and 0 <= r.exp < need and abs(bar_percent(r.exp, need) - r.pct) <= PCT_TOLERANCE + 1e-9:
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

    # --- input -------------------------------------------------------------------

    def seed(self, r: Reading) -> None:
        """Start from a reading someone else already validated (no gain booked)."""
        self._started = True
        self._anchor = self._cur = r
        self._prev = None
        self._seg = self._step = 0
        self._anchor_n = START_CONFIRM  # already validated elsewhere
        self._clear_pending()

    def add(self, r: Reading) -> int:
        """Record a reading; returns the change in session gain it causes."""
        if self._start_t is None:
            self._start_t = r.t
        self.undone = 0
        self._age += 1
        self._learning = False
        self._check_trust_back(r)
        before = self._commits
        delta = self._add(r)
        if self._commits == before:
            self.rejected_total += 1
            self._tail.append(r)
        self._record(r.t)
        return delta

    def _add(self, r: Reading) -> int:
        if r.pct is None:
            return 0
        levels = self.fits(r)
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
        """Several levels fit (EXP near 0%): the digits break the tie, but only
        for staying at the committed level. A level-up waits for a reading that
        fits one level, or for ``finalize``."""
        cur = self._cur
        if cur is not None and r.level == cur.level and cur.level in levels:
            return self._validated(Reading(r.t, cur.level, r.exp, r.pct), r)
        if cur is None and r.level in levels:
            return self._validated(Reading(r.t, r.level, r.exp, r.pct), r)
        self._ambiguous.append((r, levels))
        if cur is None and not self._started:
            self._start_candidates = [Reading(r.t, lvl, r.exp, r.pct) for lvl in levels]
        return 0

    def _validated(self, fit: Reading, raw: Reading) -> int:
        cur = self._cur
        if cur is None:
            return self._begin(fit, raw)
        d = self._dist(cur, fit)
        if self._dip is not None:
            res = self._dip_reading(fit)
            if res is not None:
                return res
        if self._suspect is not None:
            res = self._suspect_reading(fit, d)
            if res is not None:
                return res
        if fit.level != cur.level and self._young_anchor_misread(fit):
            # The anchor's percent was misread: it was this level, same EXP.
            log.info("start reading fit level %d, the bar says %d: re-anchoring", cur.level, fit.level)
            self._anchor = self._cur = Reading(cur.t, fit.level, cur.exp, cur.pct)
            self._prev = None
            return self._commit(fit, fit.exp - cur.exp)
        if d is None:
            if fit.level > cur.level:
                return self._bank_crossing(fit)
            self._suspect = _Suspect(fit, fit)
            return 0
        if d >= 0:
            return self._commit(fit, d)
        if self._is_correction(fit):
            self.undone = self._step
            return self._commit(fit, d)
        if self._death_like(fit, -d):
            self._dip = _Dip(cur, fit.exp)
            return 0
        self._suspect = _Suspect(fit, fit)
        return 0

    def _young_anchor_misread(self, fit: Reading) -> bool:
        """Right after anchoring, a reading at another level with about the
        same EXP means the anchor's percent was misread, not a level change
        (after a real one the EXP starts over)."""
        cur, anchor = self._cur, self._anchor
        if cur is None or anchor is None or cur.level != anchor.level or self._anchor_n >= START_CONFIRM:
            return False
        need = self.need(fit.level)
        return bool(need) and 0 <= fit.exp - cur.exp <= need * DEATH_MIN_FRACTION

    def _is_correction(self, fit: Reading) -> bool:
        """Does ``fit`` land back where the last rise started (that rise a misread)?"""
        if self._step <= 0 or self._prev is None or self._age > SPIKE_WINDOW:
            return False
        return self._lands_on_step_base(fit)

    def _lands_on_step_base(self, r: Reading) -> bool:
        if self._step <= 0 or self._prev is None:
            return False
        back = self._dist(self._prev, r)
        return back is not None and -self._slack(r.level) <= back <= self._step * SPIKE_LANDING

    def _death_like(self, fit: Reading, drop: int) -> bool:
        cur = self._cur
        need = self.need(fit.level)
        if cur is None or fit.level != cur.level or not need:
            return False
        if drop > need * DEATH_MAX_FRACTION + self._slack(fit.level):
            return False
        return drop >= need * DEATH_MIN_FRACTION or fit.exp == 0

    def _dip_reading(self, fit: Reading) -> int | None:
        dip = self._dip
        assert dip is not None
        ref = dip.ref
        if fit.level != ref.level or fit.exp >= ref.exp:
            self._dip = None  # back up: the drop was a misread
            return None
        if not self._death_like(fit, ref.exp - fit.exp) and fit.exp < dip.low:
            return 0  # inconsistent with the dip: ignore it
        dip.n += 1
        dip.low = min(dip.low, fit.exp)
        if dip.n < DEATH_CONFIRM:
            return 0
        loss = ref.exp - dip.low
        self.lost += loss
        self._dip = None
        log.info("death at level %d: -%d EXP", fit.level, loss)
        return self._commit(fit, fit.exp - ref.exp) + loss

    def _suspect_reading(self, fit: Reading, d: int | None) -> int | None:
        s = self._suspect
        assert s is not None
        if d is None or d >= 0:
            self._suspect = None  # back at (or above) the committed position
            return None
        step = self._dist(s.last, fit)
        if fit.level == s.last.level and step is not None and step >= -self._slack(fit.level):
            s.last = fit
            s.n += 1
        else:
            self._suspect = _Suspect(fit, fit)
            return 0
        if s.n < SUSPECT_CONFIRM:
            return 0
        self._suspect = None
        return self._confirm_suspect(s)

    def _confirm_suspect(self, s: _Suspect) -> int:
        cur = self._cur
        assert cur is not None
        if self._lands_on_step_base(s.first):  # a late correction of the last rise
            d = self._dist(cur, s.last)
            if d is not None:
                self.undone = self._step
                return self._commit(s.last, d)
        # The committed position itself looks wrong (a bad anchor, or an
        # unexplained jump): keep what was gained and continue from here.
        log.warning(
            "EXP bar moved from L%d %d to L%d %d and stayed; re-anchoring",
            cur.level, cur.exp, s.first.level, s.first.exp,
        )
        self._rebase(s.first)
        d = self._dist(s.first, s.last) or 0
        return self._commit(s.last, d) if d > 0 else 0

    def _begin(self, fit: Reading, raw: Reading) -> int:
        """First validated reading of a segment (session start, or after a pause)."""
        start = fit
        if not self._started:
            if raw.level != fit.level:  # wait for a reading the digits agree with
                self._start_disagree.append(fit)
                same = [f for f in self._start_disagree if f.level == fit.level]
                if len(same) < START_CONFIRM:
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
        self._anchor_n = 1
        self._commits += 1
        self._clear_pending()
        if start is fit:
            return 0
        return self._commit(fit, self._dist(start, fit) or 0)

    def _rebase(self, r: Reading) -> None:
        """Close the segment (keeping its gain) and start a new one at ``r``."""
        if self._cur is not None and self._anchor is not None:
            self._banked += self._seg
            self._levels_banked += self._cur.level - self._anchor.level
        self._anchor = self._cur = r
        self._prev = None
        self._seg = self._step = 0
        self._anchor_n = 1
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
        if self._anchor is not None and r.level == self._anchor.level:
            self._anchor_n += 1
        self._prev, self._cur = self._cur, r
        self._seg += delta
        if delta > 0:
            self._step, self._age = delta, 0
        elif delta < 0:
            self._step = 0
        self._commits += 1
        self._clear_pending()
        return delta

    def _clear_pending(self) -> None:
        self._tail = []
        self._ambiguous = []
        self._dip = None
        self._suspect = None

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
        ending). Returns the change in gain."""
        cur = self._cur
        if cur is None:
            return 0
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
            if d is not None and d >= 0:
                return self._commit(c, d)
        if not all(cur.level in lv for _, lv in amb):
            return 0
        self._ambiguous = []
        delta = 0
        for a, _ in amb:  # replay them at the committed level: no change, or a death
            c = Reading(a.t, cur.level, a.exp, a.pct)
            delta += self._validated(c, c)
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
