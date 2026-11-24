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

Misreads repeat while the bar is still: OCR of an unchanged frame returns the
same wrong text every time, so a repeat is not evidence. Four rules follow:

A. A level change needs the digits: they must say the fitted level (or, on a
   +1, be unreadable or the new level with a digit dropped, "9" for 90).
   MapleStory can't de-level, so a fit below the committed level is a misread
   and is ignored, however long it persists.
B. A big same-level move needs a changing frame: a rise above BIG_FRACTION of
   the level and BIG_RATE_FACTOR x the recent per-minute rate is held until a
   reading with *different* raw EXP agrees with it. Suspect drops likewise
   need SUSPECT_CONFIRM distinct raw EXP values. Identical repeats never count.
C. Spikes are measured from the right base: a repeat of the committed reading
   never becomes ``previous``, and the spike window only ages on changed frames,
   so a spike read twice is still taken back from the pre-spike position.
D. No re-anchoring onto another level except under rule A. A reading back at
   the committed position cancels a pending big rise or suspect drop.

Validated readings are then checked against the committed position:

* up: committed at once, unless rule B holds it. If one of the next two
  changed frames falls back to about where the step started, the step was a
  misread and is taken back (a spike correction).
* down, same level, 1-10% of the level (or down to exactly 0): a possible
  death, booked once DEATH_CONFIRM readings show it. Deaths do confirm on
  identical repeats: after dying the bar is still, and no single-glyph misread
  fits the same level that far down. A town death (need // 100) counts.
* any other same-level drop: held as *suspect*. After SUSPECT_CONFIRM distinct
  readings the tracker either takes back the last rise (only while that rise was
  still unconfirmed) or re-anchors at the same level without booking a loss.

Interface used by the kill counter (PR #9)
------------------------------------------
``verified_gain``
    Committed position change plus confirmed deaths, excluding the last rise
    while it is inside its spike window, what ``finalize`` books from an
    unreadable-percent tail, and anything pending. It never goes down (deaths
    are in ``lost`` and stay in it).
``unconfirmed_gain``
    The committed rise not in ``verified_gain`` yet; ``verified_gain +
    unconfirmed_gain`` is what the tracker currently believes was gained
    (bar the finalize tail). On a still bar a fresh rise stays here until the
    frame changes twice.
``behind``
    True while the tracker has seen readings it hasn't committed: any rejected
    reading since the last commit (unreadable percent, no fit, a misread level),
    a pending ambiguous reading, a big rise waiting for a changed frame, a
    suspect drop, an unconfirmed death dip, a level being learned, or a start
    not anchored yet. EXP the messages show may legitimately be ahead of
    ``verified_gain + unconfirmed_gain`` then.
``last_committed_t``
    When the last reading was committed, in the clock of the ``now`` passed to
    ``add``/``seed``. ``Session`` passes ``time.monotonic()``, the clock message
    timestamps use (``Reading.t`` itself is the session's active-seconds clock).
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
DEATH_MIN_FRACTION = 0.01  # town death; less one percent step (need // 100 < need/100)
DEATH_CONFIRM = 3
# A rise is a misread if one of the next SPIKE_WINDOW *changed frames* (raw
# EXP different from the step's) lands back within SPIKE_LANDING of the step
# above where it started. Identical repeats of the step don't age it.
SPIKE_WINDOW = 2
SPIKE_LANDING = 0.25
# A same-level rise bigger than BIG_FRACTION of the level and BIG_RATE_FACTOR x
# the recent per-minute rate is held until BIG_CONFIRM distinct raw EXP values
# (the first included) agree with it.
BIG_FRACTION = 0.10
BIG_RATE_FACTOR = 20
BIG_CONFIRM = 2
# Distinct raw EXP values a suspect drop must show before the tracker follows it.
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
    exps: set[int] = field(default_factory=set)  # distinct raw EXP values seen
    undo_ok: bool = False  # the last rise was still unconfirmed when it began


@dataclass
class _Big:
    first: Reading  # the implausibly big rise, not committed
    step: int  # its size from the committed position
    exps: set[int] = field(default_factory=set)


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
    _step: int = field(default=0, init=False)  # last rise (0 once it was taken back)
    _step_raw: int | None = field(default=None, init=False)  # raw EXP of that rise
    _step_frames: set[int] = field(default_factory=set, init=False)  # other raw EXP seen since
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
    _big: _Big | None = field(default=None, init=False)
    _now: float | None = field(default=None, init=False)  # caller's clock of the reading
    _committed_at: float | None = field(default=None, init=False)
    _verified_hw: int = field(default=0, init=False)  # verified_gain never goes down
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
        """Committed position change plus confirmed deaths, minus a rise still
        inside its spike window (see module doc). Never goes down."""
        self._verified_hw = max(self._verified_hw, self.gained - self._tail_booked - self._unconfirmed_step())
        return self._verified_hw

    def _step_age(self) -> int:
        return len(self._step_frames)

    def _unconfirmed_step(self) -> int:
        """The last rise, while a later reading could still take it back."""
        if self._step <= 0:
            return 0
        if self._step_age() < SPIKE_WINDOW or (self._suspect is not None and self._suspect.undo_ok):
            return self._step
        return 0

    @property
    def behind(self) -> bool:
        """True while a reading is pending that may still move the position."""
        return bool(
            self._ambiguous
            or self._suspect is not None
            or self._dip is not None
            or self._big is not None
            or self._learning
            or self._start_disagree
            or self._tail
        )

    @property
    def unconfirmed_gain(self) -> int:
        """Committed gain not in ``verified_gain`` yet: the last rise while it
        is inside its spike window (it may still be taken back)."""
        return self._unconfirmed_step()

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

    def seed(self, r: Reading, now: float | None = None) -> None:
        """Start from a reading someone else already validated (no gain booked)."""
        self._started = True
        self._anchor = self._cur = r
        self._prev = None
        self._seg = self._step = 0
        self._anchor_n = START_CONFIRM  # already validated elsewhere
        self._committed_at = r.t if now is None else now
        self._clear_pending()

    def add(self, r: Reading, now: float | None = None) -> int:
        """Record a reading; returns the change in session gain it causes.
        ``now`` is the caller's clock for ``last_committed_t`` (default ``r.t``)."""
        if self._start_t is None:
            self._start_t = r.t
        self._now = r.t if now is None else now
        self.undone = 0
        if self._step_raw is not None and r.exp != self._step_raw:
            self._step_frames.add(r.exp)  # a changed frame ages the last rise
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
        if fit.level != cur.level:
            if self._young_anchor_misread(fit, raw):
                # The anchor's percent was misread: it was this level, same EXP.
                log.info("start reading fit level %d, the bar says %d: re-anchoring", cur.level, fit.level)
                self._anchor = self._cur = Reading(cur.t, fit.level, cur.exp, cur.pct)
                self._prev = None
                return self._commit(fit, fit.exp - cur.exp)
            if not self._level_change_ok(fit, raw):
                return 0  # rule A: a misread; it never moves anything
        d = self._dist(cur, fit)
        if self._big is not None:
            res = self._big_reading(fit, raw, d)
            if res is not None:
                return res
        if self._dip is not None:
            res = self._dip_reading(fit)
            if res is not None:
                return res
        if self._suspect is not None:
            res = self._suspect_reading(fit, raw, d)
            if res is not None:
                return res
        if d is None:  # a level-up past a level of unknown size
            return self._bank_crossing(fit)
        if d >= 0:
            if fit.level == cur.level and d > self._big_threshold(fit.level, fit.t - cur.t):
                self._big = _Big(fit, d, {raw.exp})  # rule B: wait for a changed frame
                return 0
            return self._commit(fit, d)
        if self._is_correction(fit):
            self.undone = self._step
            return self._commit(fit, d)
        if self._death_like(fit, -d):
            self._dip = _Dip(cur, fit.exp)
            return 0
        self._suspect = _Suspect(fit, fit, exps={raw.exp}, undo_ok=self._unconfirmed_step() > 0)
        return 0

    def _level_change_ok(self, fit: Reading, raw: Reading) -> bool:
        """Rule A: a level change needs the digits to say the fitted level, or,
        on a +1, to be unreadable (0) or the new level with a digit dropped or
        doubled ("9" for 90). MapleStory can't de-level: a lower fit is a misread."""
        cur = self._cur
        assert cur is not None
        if fit.level < cur.level:
            log.debug("L%d %d fits L%d below the committed L%d: misread", raw.level, raw.exp, fit.level, cur.level)
            return False
        if raw.level == fit.level:
            return True
        return fit.level == cur.level + 1 and (
            raw.level <= 0 or (raw.level != cur.level and raw.level in {fit.level // 10, fit.level * 10})
        )

    def _big_threshold(self, level: int, dt: float) -> float:
        """Largest same-level rise that plausible play explains in ``dt``
        seconds: per minute (or per reading, if sooner), BIG_FRACTION of the
        level or BIG_RATE_FACTOR x the recent per-minute rate."""
        need = self.need(level)
        if not need:
            return float("inf")
        per_min = max(need * BIG_FRACTION, BIG_RATE_FACTOR * self._recent_rate_per_min())
        return per_min * max(1.0, dt / 60)

    def _recent_rate_per_min(self) -> float:
        """Committed EXP per minute over the rolling window, once it spans a minute."""
        if len(self._history) < 2 or self._history[-1][0] - self._history[0][0] < 60:
            return 0.0
        return max(0.0, self.rate_per_hour() / 60)

    def _big_reading(self, fit: Reading, raw: Reading, d: int | None) -> int | None:
        """A reading while an implausibly big rise waits for a changed frame."""
        b = self._big
        assert b is not None
        if d is not None and -self._slack(fit.level) <= d <= b.step * SPIKE_LANDING:
            self._big = None  # rules C/D: back at the committed position, it was a misread
            return None
        up = self._dist(b.first, fit)
        if fit.level == b.first.level and up is not None and (
            up >= -self._slack(fit.level) or self._death_like(fit, -up, ref=b.first)
        ):
            b.exps.add(raw.exp)  # identical repeats don't add evidence
            if len(b.exps) < BIG_CONFIRM:
                return 0
            self._big = None
            log.info("big rise at level %d confirmed: +%d EXP", fit.level, b.step)
            delta = self._commit(b.first, b.step)
            return delta + self._validated(fit, raw)
        self._big = None  # inconsistent with it: judge the reading on its own
        return None

    def _young_anchor_misread(self, fit: Reading, raw: Reading) -> bool:
        """Right after anchoring, a reading at another level with about the
        same EXP means the anchor's percent was misread, not a level change
        (after a real one the EXP starts over). The digits must agree (rule A)."""
        cur, anchor = self._cur, self._anchor
        if cur is None or anchor is None or cur.level != anchor.level or self._anchor_n >= START_CONFIRM:
            return False
        if raw.level != fit.level:
            return False
        need = self.need(fit.level)
        return bool(need) and 0 <= fit.exp - cur.exp <= need * DEATH_MIN_FRACTION

    def _is_correction(self, fit: Reading) -> bool:
        """Does ``fit`` land back where the last rise started (that rise a misread)?"""
        if self._step <= 0 or self._prev is None or self._step_age() > SPIKE_WINDOW:
            return False
        return self._lands_on_step_base(fit)

    def _lands_on_step_base(self, r: Reading) -> bool:
        if self._step <= 0 or self._prev is None:
            return False
        back = self._dist(self._prev, r)
        return back is not None and -self._slack(r.level) <= back <= self._step * SPIKE_LANDING

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

    def _suspect_reading(self, fit: Reading, raw: Reading, d: int | None) -> int | None:
        s = self._suspect
        assert s is not None
        if d is None or d >= 0:
            self._suspect = None  # back at (or above) the committed position
            return None
        step = self._dist(s.last, fit)
        if fit.level == s.last.level and step is not None and step >= -self._slack(fit.level):
            s.last = fit
            s.n += 1
            s.exps.add(raw.exp)  # rule B: only changed frames confirm it
        else:
            self._suspect = _Suspect(fit, fit, exps={raw.exp}, undo_ok=s.undo_ok)
            return 0
        if len(s.exps) < SUSPECT_CONFIRM:
            return 0
        self._suspect = None
        return self._confirm_suspect(s)

    def _confirm_suspect(self, s: _Suspect) -> int:
        cur = self._cur
        assert cur is not None
        if s.undo_ok and self._lands_on_step_base(s.first):  # a late correction of the last rise
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
        self._anchor_n = 1
        self._commits += 1
        self._committed_at = self._now
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
        cur = self._cur
        if delta == 0 and cur is not None and (cur.level, cur.exp) == (r.level, r.exp):
            self._cur = r  # a repeat: rule C, it never becomes the base (_prev)
        else:
            self._prev, self._cur = cur, r
        self._seg += delta
        if delta > 0:
            self._step, self._step_raw, self._step_frames = delta, r.exp, set()
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
        self._suspect = None
        self._big = None

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
        ending). Returns the change in gain. A pending big rise is not booked;
        the last rise counts as confirmed from here on."""
        delta = self._finalize()
        self._step = 0
        return delta

    def _finalize(self) -> int:
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
