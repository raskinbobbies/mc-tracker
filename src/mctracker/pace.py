"""Pace vs your record here: the live session's EXP/hour against your saved sessions on the same map.

No Qt in here. ``records`` runs once per history (re)load, never per frame; ``compare`` is a dict lookup
and some arithmetic, cheap enough for the 1 s refresh. Saved sessions don't record the character, so
every session on the map counts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median

MIN_SAVED_S = 10 * 60  # shorter saved sessions are too noisy to be a record
MIN_LIVE_S = 4 * 60  # nothing is shown until the live session has run this long
NEAR_PCT = 2  # within +/-2% of the best reads as "matching your best"


@dataclass(frozen=True)
class MapRecord:
    best: float  # EXP/hour
    median: float
    sessions: int


@dataclass(frozen=True)
class Pace:
    pct: int  # vs ``ref`` (+ ahead, - behind)
    ref: str  # "best" or "median"
    ref_rate: float  # EXP/hour
    tone: str  # "over" / "near" / "under" (the pill tones)
    record: MapRecord


def saved_rate(data: dict) -> float | None:
    """A saved session's EXP/hour, or None when it can't count (too short, or no positive rate).

    Prefers the saved ``exp_per_hour``; older files without it use net EXP over the active time.
    Deaths can make the saved figure negative (a known bug), so non-positive rates are skipped."""
    try:
        seconds = float(data.get("active_seconds") or 0)
        if seconds < MIN_SAVED_S:
            return None
        rate = data.get("exp_per_hour")
        if rate is None:
            rate = (float(data.get("exp_gained") or 0) - float(data.get("exp_lost") or 0)) / seconds * 3600
        rate = float(rate)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def records(sessions: Iterable) -> dict[str, MapRecord]:
    """Best and median EXP/hour per map over the saved sessions that count (see ``saved_rate``).

    ``sessions`` are ``history.SavedSession``s (anything with ``.data``); a map with none that count
    has no entry."""
    rates: dict[str, list[float]] = {}
    for s in sessions:
        name = s.data.get("map")
        rate = saved_rate(s.data)
        if name and rate is not None:
            rates.setdefault(name, []).append(rate)
    return {m: MapRecord(max(r), median(r), len(r)) for m, r in rates.items()}


def compare(record: MapRecord | None, live_rate: float, active_s: float) -> Pace | None:
    """The live session's EXP/hour against the map's record (None: show nothing).

    At or above your median you're chasing your best, so the figure is vs the best; below the median
    it's vs the median, a fairer bar on a slow day (with one session the two are the same: "best").
    Green ahead of your best, grey between median and best, red under the median."""
    if record is None or active_s < MIN_LIVE_S or live_rate <= 0:
        return None
    by_best = live_rate >= record.median or record.sessions < 2
    ref, ref_rate = ("best", record.best) if by_best else ("median", record.median)
    pct = round((live_rate - ref_rate) / ref_rate * 100)
    tone = "over" if pct > NEAR_PCT else "near" if pct >= -NEAR_PCT or live_rate >= record.median else "under"
    return Pace(pct, ref, ref_rate, tone, record)


def fmt_rate(n: float) -> str:
    """Compact EXP/hour: "1.42M/h", "125.3K/h", "8,400/h"."""
    if n >= 1e6:
        return f"{n / 1e6:.2f}M/h"
    if n >= 1e5:
        return f"{n / 1e3:.1f}K/h"
    return f"{round(n):,}/h"


def _lead(p: Pace) -> str:
    """"▲ 12% vs", "▼ 8% vs", or "≈" within NEAR_PCT."""
    if abs(p.pct) <= NEAR_PCT:
        return "≈"
    return f"▲ {p.pct}% vs" if p.pct > 0 else f"▼ {-p.pct}% vs"


def pace_text(p: Pace) -> str:
    """The live tab's pill: "▲ 12% vs your best here (1.42M/h)", "≈ your best here (1.42M/h)"."""
    return f"{_lead(p)} your {p.ref} here ({fmt_rate(p.ref_rate)})"


def pace_short(p: Pace) -> str:
    """The overlay's compact form: "▲12% vs best", "≈ best"."""
    return f"{_lead(p).replace(' ', '', 1)} {p.ref}"  # "▲ 12% vs" -> "▲12% vs"; "≈" stays


def pace_tip(p: Pace) -> str:
    n = p.record.sessions
    return (f"This session's EXP/hour vs your saved sessions on this map\n"
            f"({n} session{'s' if n != 1 else ''} of {MIN_SAVED_S // 60}+ min): "
            f"best {fmt_rate(p.record.best)}, median {fmt_rate(p.record.median)}")
