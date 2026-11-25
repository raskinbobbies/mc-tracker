"""Last-minute vs session-average comparison, shared by the main window's pill and the overlay badge.

No Qt in here, so both can use it and the rules are easy to test.
"""

from __future__ import annotations

PILL_NEAR_PCT = 2  # within +/-2% (as rounded and shown) reads as "at your average"


def last_minute_delta(minutes: list[int], avg_per_min: float) -> int | None:
    """Percent the last *finished* minute is above (+) or below (-) the session average.

    ``minutes`` is ``Session.exp_per_minute``, whose last entry is the current partial
    minute, so it is dropped. ``avg_per_min`` should exclude the judged minute (see
    ``average_before_last_minute``). None until two minutes have finished or while there
    is no average to compare against.
    """
    finished = minutes[:-1]
    if len(finished) < 2 or avg_per_min <= 0:
        return None
    return round((finished[-1] - avg_per_min) / avg_per_min * 100)


def delta_tone(pct: int) -> str:
    """"over", "under" or "near" for a ``last_minute_delta`` figure."""
    if abs(pct) <= PILL_NEAR_PCT:
        return "near"
    return "over" if pct > 0 else "under"


def last_minute_pill(minutes: list[int], avg_per_min: float) -> tuple[str, str] | None:
    """(text, tone) comparing the last finished minute with the session average (None: hide it)."""
    pct = last_minute_delta(minutes, avg_per_min)  # band and text use the same figure
    if pct is None:
        return None
    tone = delta_tone(pct)
    if tone == "near":
        return "at your average", tone
    return (f"{pct}% over your average" if pct > 0 else f"{-pct}% under your average"), tone


def average_before_last_minute(net_exp: float, active_s: float, last_minute_exp: float) -> float:
    """EXP/min over the session excluding the finished minute the pill judges (0 if nothing else yet)."""
    rest_s = active_s - 60
    return (net_exp - last_minute_exp) / rest_s * 60 if rest_s > 0 else 0.0
