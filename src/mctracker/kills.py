"""Infer party kills from EXP gain messages.

Every monster your party kills on your map gives you a message, whoever
killed it (confirmed in game):

    You received EXP (+39)
    Bonus EXP for PARTY (+4)      <- only when in a party

So: count new "You received EXP" lines -> kills, and identify each line's
monster by matching its EXP against the current map's monsters.

Solo, the value equals the monster's EXP (times any event bonus), so we fit one
multiplicative scale ``k`` and assign each value to the nearest ``k * base_exp``
in log space. In a party your share varies per kill (damage split), so party
kills are only identified when the map's monsters are far enough apart in EXP.
"""

from __future__ import annotations

import math
import re
from itertools import combinations
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz import fuzz

EXP_RE = re.compile(r"received\s*EXP\s*\(\s*\+\s*([\d,.]+)", re.IGNORECASE)
PARTY_BONUS_RE = re.compile(r"Bonus\s*EXP\s*for\s*PARTY\s*\(\s*\+\s*([\d,.]+)", re.IGNORECASE)
# "You have gained mesos (+10)"; OCR may drop spaces or read 's' as '5'.
MESOS_RE = re.compile(r"gained\s*m[e3][s5][o0][s5]?\s*\(?\s*\+\s*(\d[\d,.\s]*)", re.IGNORECASE)
LOOT_RE = re.compile(r"^\s*(.+?)\s*[x×]\s*(\d+)\s*earned\.?\s*\(\s*(\w+)\s*\)", re.IGNORECASE)


def _int(s: str) -> int | None:
    d = re.sub(r"\D", "", s)
    return int(d) if d else None


@dataclass(frozen=True)
class Message:
    kind: str  # "exp" | "party_bonus" | "loot" | "mesos"
    value: int = 0  # EXP amount, loot quantity or mesos
    item: str = ""
    category: str = ""


def parse_message(text: str) -> Message | None:
    if m := PARTY_BONUS_RE.search(text):
        v = _int(m.group(1))
        return Message("party_bonus", v) if v is not None else None
    if m := EXP_RE.search(text):
        v = _int(m.group(1))
        return Message("exp", v) if v is not None else None
    if m := MESOS_RE.search(text):
        v = _int(m.group(1))
        return Message("mesos", v) if v else None
    if m := LOOT_RE.search(text):
        return Message("loot", int(m.group(2)), m.group(1).strip(), m.group(3))
    return None


def parse_exp_line(text: str) -> int | None:
    m = parse_message(text)
    return m.value if m and m.kind == "exp" else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", s.lower())


def _same(a: str, b: str) -> bool:
    """Equal text, tolerating OCR noise in letters but never in numbers."""
    if a == b:
        return True
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return False
    return len(a) > 6 and fuzz.ratio(a, b) >= 90


class LineTracker:
    """Detect newly appeared lines in a bottom-anchored scrolling message area.

    New lines appear at the bottom and push older lines up by one line height;
    old lines fade from the top. Given lines as (text, y_center), we find the
    shift ``k`` (number of new lines) that best explains the new frame:
    previous lines should reappear ``k`` slots higher, and every line not in
    the bottom ``k`` slots must have been seen before. This correctly counts
    repeated identical lines (e.g. two "+82" kills) and tolerates OCR dropping
    or fading a line.

    Plain strings are accepted too (treated as evenly spaced, bottom-aligned).
    """

    def __init__(self, line_height: float | None = None) -> None:
        self.prev: list[tuple[str, float]] = []
        self.h = line_height

    @staticmethod
    def _coerce(lines) -> list[tuple[str, str, float]]:
        out = []
        n = len(lines)
        for i, l in enumerate(lines):
            if isinstance(l, str):
                text, y = l, float(i - n + 1)
            elif isinstance(l, tuple):
                text, y = l[0], float(l[1])
            else:
                text, y = l.text, float(l.y)
            out.append((text, _norm(text), y))
        out.sort(key=lambda t: t[2])
        return out

    def _height(self, *frames) -> float:
        if self.h:
            return self.h
        diffs = sorted(
            b[-1] - a[-1]
            for f in frames
            for a, b in zip(f, f[1:])
            if b[-1] - a[-1] > 0
        )
        return diffs[len(diffs) // 2] if diffs else 1.0

    def update(self, lines) -> list[str]:
        cur = self._coerce(lines)
        prev = self.prev
        self.prev = cur
        if not prev:
            return [c[0] for c in cur]
        if not cur:
            return []
        h = self._height(cur, prev)
        tol = 0.35 * h
        best_k, best_score = len(cur), -math.inf
        for k in range(len(cur) + 1):
            old_part = cur[: len(cur) - k]
            matched = 0
            used = set()
            for _, pn, py in prev:
                want = py - k * h
                for j, (_, cn, cy) in enumerate(old_part):
                    if j not in used and abs(cy - want) <= tol and _same(pn, cn):
                        used.add(j)
                        matched += 1
                        break
            unexplained = len(old_part) - len(used)
            score = matched - unexplained
            if score > best_score:
                best_k, best_score = k, score
        return [c[0] for c in cur[len(cur) - best_k:]] if best_k else []


@dataclass(frozen=True)
class Mob:
    id: int
    name: str
    exp: int


@dataclass
class KillCounter:
    mobs: list[Mob] = field(default_factory=list)
    # Max observations used to fit the scale (recent ones matter most).
    fit_window: int = 200
    # Values further than this from any monster (in log units) are "unknown".
    max_log_error: float = math.log(1.6)

    values: list[int] = field(default_factory=list, init=False)
    party: list[bool] = field(default_factory=list, init=False)
    scale: float = field(default=1.0, init=False)
    _dirty: bool = field(default=False, init=False)

    def set_map(self, mobs: Sequence[Mob]) -> None:
        """Start a new map segment (counts so far are kept by the caller)."""
        self.mobs = [m for m in mobs if m.exp > 0]
        self.values.clear()
        self.party.clear()
        self.scale = 1.0
        self._dirty = False

    def add(self, exp_value: int, party: bool = False) -> None:
        if exp_value > 0:
            self.values.append(exp_value)
            self.party.append(party)
            self._dirty = True

    def mark_last_party(self) -> None:
        if self.party:
            self.party[-1] = True
            self._dirty = True

    def party_identifiable(self) -> bool:
        """Party shares vary ~±50%, so only trust IDs when monsters' EXP are far apart."""
        exps = sorted({m.exp for m in self.mobs})
        return all(b / a >= 2.0 for a, b in zip(exps, exps[1:]))

    @property
    def total(self) -> int:
        return len(self.values)

    # Solo EXP from one monster type is the same number every kill, so values
    # within this relative tolerance are treated as one monster type.
    cluster_tol: float = 0.04
    # Pull toward 1x scale (no event / no level penalty) unless data says otherwise.
    prior_weight: float = 0.02

    def _clusters(self, values: list[int]) -> list[tuple[float, int]]:
        """Group near-identical values -> [(center, count)], ascending."""
        out: list[list[float]] = []
        for v in sorted(values):
            if out and v <= out[-1][0] * (1 + self.cluster_tol):
                out[-1].append(v)
            else:
                out.append([v])
        return [(sorted(c)[len(c) // 2], len(c)) for c in out]

    def _fit(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        self._assign: list[tuple[float, Mob]] = []
        solo = [v for v, p in zip(self.values, self.party) if not p]
        obs = (solo or self.values)[-self.fit_window:]
        mobs = sorted({m.exp: m for m in self.mobs}.values(), key=lambda m: m.exp)
        if not obs or not mobs:
            return
        clusters = self._clusters(obs)
        if len(clusters) > len(mobs) or len(clusters) > 8:
            self._fit_scale_only(obs)
            return
        # Each distinct value is a different monster: try every order-preserving
        # one-to-one assignment and keep the one with the most consistent scale.
        # Clusters that fit no monster (OCR garbage, unlisted monsters) are
        # dropped one at a time, worst first.
        while clusters:
            n = sum(c for _, c in clusters)
            best = None
            for combo in combinations(mobs, len(clusters)):
                ratios = [(math.log(v) - math.log(m.exp), c) for (v, c), m in zip(clusters, combo)]
                lk = sum(r * c for r, c in ratios) / n
                cost = sum(abs(r - lk) * c for r, c in ratios) + self.prior_weight * n * abs(lk)
                if best is None or cost < best[0]:
                    best = (cost, lk, combo, ratios)
            _, lk, combo, ratios = best
            worst = max(range(len(ratios)), key=lambda i: abs(ratios[i][0] - lk))
            if len(clusters) > 1 and abs(ratios[worst][0] - lk) > self.max_log_error:
                clusters = clusters[:worst] + clusters[worst + 1:]
                continue
            self.scale = math.exp(lk)
            self._assign = [(v, m) for (v, _), m in zip(clusters, combo)]
            return

    def _fit_scale_only(self, obs: list[int]) -> None:
        logs_b = [math.log(m.exp) for m in self.mobs]
        logs_o = [math.log(v) for v in obs]
        cap = self.max_log_error
        cands = {round(lo - lb, 4) for lo in set(logs_o) for lb in logs_b}
        best, best_cost = 0.0, float("inf")
        for lk in cands:
            cost = sum(min(cap, min(abs(lo - lk - lb) for lb in logs_b)) for lo in logs_o)
            cost += self.prior_weight * len(logs_o) * abs(lk)
            if cost < best_cost:
                best, best_cost = lk, cost
        self.scale = math.exp(best)

    def classify(self, exp_value: int) -> Mob | None:
        self._fit()
        if not self.mobs or exp_value <= 0:
            return None
        for v, mob in getattr(self, "_assign", []):
            if abs(exp_value - v) <= v * self.cluster_tol:
                return mob
        lo = math.log(exp_value) - math.log(self.scale)
        mob = min(self.mobs, key=lambda m: abs(lo - math.log(m.exp)))
        return mob if abs(lo - math.log(mob.exp)) <= self.max_log_error else None

    def counts(self) -> Counter[int | None]:
        """Kills per mob id for this map segment (None = unidentified)."""
        self._fit()
        trust_party = self.party_identifiable()
        return Counter(
            (m.id if (not p or trust_party) and (m := self.classify(v)) else None)
            for v, p in zip(self.values, self.party)
        )


def coverage(message_sum: int, bar_delta: int) -> float | None:
    """Fraction of EXP-bar gain explained by messages we read (1.0 = nothing missed)."""
    if bar_delta <= 0:
        return None
    return message_sum / bar_delta


def estimate_missed(message_sum: int, bar_delta: int, kills: int) -> int:
    """Kills probably missed (lines scrolled away / ambiguous duplicates)."""
    if kills == 0 or bar_delta <= message_sum:
        return 0
    avg = message_sum / kills
    return round((bar_delta - message_sum) / avg)
