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
# "Pumpkin Piece x1 earned. (Etc)". OCR often reads "x1" as "xl" and "earned" as
# "eamed"/"eared", so both are tolerated.
LOOT_RE = re.compile(
    r"^\s*(.+?)\s*[x×]\s*([\dlI|]+)\s*ea[rnm]{1,3}ed\.?\s*,?\s*\(\s*(\w+)\s*\)", re.IGNORECASE
)
# Party system messages: "You have created a new party.", "<name> has joined the
# party.", "You have left the party.", "The party has been disbanded."
PARTY_EVENT_RES = (
    ("created", re.compile(r"created\s+a\s+new\s+party", re.IGNORECASE)),
    ("disbanded", re.compile(r"disbanded", re.IGNORECASE)),
    ("joined", re.compile(r"^\s*(.*?)\s*(?:has|have)?\s*joined\s+the\s+party", re.IGNORECASE)),
    ("left", re.compile(r"^\s*(.*?)\s*(?:has|have)?\s*left\s+the\s+party", re.IGNORECASE)),
)


def _int(s: str) -> int | None:
    d = re.sub(r"\D", "", s.replace("l", "1").replace("I", "1").replace("|", "1"))
    return int(d) if d else None


@dataclass(frozen=True)
class Message:
    kind: str  # "exp" | "party_bonus" | "loot" | "party"
    value: int = 0  # EXP amount or loot quantity
    item: str = ""  # loot: item name; party: who ("you" or a name)
    category: str = ""  # loot: tab; party: created | joined | left | disbanded


def parse_message(text: str) -> Message | None:
    if m := PARTY_BONUS_RE.search(text):
        v = _int(m.group(1))
        return Message("party_bonus", v) if v is not None else None
    if m := EXP_RE.search(text):
        v = _int(m.group(1))
        return Message("exp", v) if v is not None else None
    if m := LOOT_RE.search(text):
        n = _int(m.group(2))
        return Message("loot", n, m.group(1).strip(), m.group(3)) if n else None
    for event, rx in PARTY_EVENT_RES:
        if m := rx.search(text):
            who = (m.group(1) if m.groups() else "").strip()
            who = "you" if who.lower() in ("", "you") else who
            return Message("party", 0, who, event)
    return None


def parse_exp_line(text: str) -> int | None:
    m = parse_message(text)
    return m.value if m and m.kind == "exp" else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", s.lower())


def _likeness(a: Message, b: Message) -> int:
    """Two reads of the same on-screen message? 1 = same, 0 = probably (same
    numbers, item name too garbled to tell), -1 = different. Numbers must match
    exactly; item names may differ by OCR noise ("Pumpkin" / "Purpkin")."""
    if (a.kind, a.value) != (b.kind, b.value):
        return -1
    if a.kind == "party" and a.category != b.category:
        return -1
    if a.kind not in ("loot", "party"):
        return 1
    x, y = _norm(a.item), _norm(b.item)
    return 1 if x == y or fuzz.ratio(x, y) >= 75 else 0


@dataclass
class _Line:
    text: str
    msg: Message
    y: float
    unseen: int = 0  # consecutive updates this line was not read


class LineTracker:
    """Detect newly appeared messages in a bottom-anchored scrolling area.

    New messages appear in the bottom slot and push older ones up by one line
    pitch ``h``; old ones fade from the top. Each update we find the shift ``k``
    (number of new messages) that best explains what is on screen now.

    Only lines that parse as messages take part. In the real game the area is
    full of noise that must never look like a new message (see
    tests/fixtures/burnt_land_v_messages.txt):

    * damage numbers from nearby fights float through it ("219", "1074");
    * a fading line reads as fragments ("ved EXP (+43)", "Yourer", "(43)");
    * a line can drop out for a frame (occluded) and come back;
    * OCR flickers in letters ("ExP", "recelved", "Purpkin").

    So matching compares parsed values, not raw text, and lines stay in memory
    for ``keep`` updates after they were last read, so a line that reappears
    is recognised instead of being counted again. Identical lines ("+43" twice)
    are still counted, by position.

    Lines are TextLine-like objects (text, y, height), (text, y) tuples, or
    plain strings (treated as evenly spaced, bottom-aligned).
    """

    def __init__(self, line_height: float | None = None, keep: int = 3) -> None:
        self.mem: list[_Line] = []
        self.h = line_height  # fixed pitch, if known
        self._h_est: float | None = None  # learned pitch
        self.keep = keep

    @staticmethod
    def _coerce(lines) -> list[tuple[str, float, float]]:
        """-> [(text, y, box_height)], same-row fragments merged, top to bottom."""
        raw = []
        n = len(lines)
        for i, l in enumerate(lines):
            if isinstance(l, str):
                raw.append((l, float(i - n + 1), 0.0, 0.0))
            elif isinstance(l, tuple):
                raw.append((l[0], float(l[1]), 0.0, 0.0))
            else:
                raw.append((l.text, float(l.y), float(getattr(l, "height", 0) or 0),
                            float(getattr(l, "x0", 0) or 0)))
        raw.sort(key=lambda t: t[1])
        rows: list[list[tuple[str, float, float, float]]] = []
        for r in raw:
            # Same row: OCR sometimes splits one line into fragments.
            if rows and r[2] and rows[-1][0][2] and abs(r[1] - rows[-1][0][1]) < 0.3 * min(r[2], rows[-1][0][2]):
                rows[-1].append(r)
            else:
                rows.append([r])
        out = []
        for row in rows:
            row.sort(key=lambda t: t[3])
            out.append((" ".join(t[0] for t in row), row[0][1], max(t[2] for t in row)))
        return out

    def _pitch(self, cur: list[_Line], boxes: list[float]) -> float:
        if self.h:
            return self.h
        ys = sorted(l.y for l in cur)
        guess = self._h_est or (sorted(boxes)[len(boxes) // 2] if boxes else None)
        diffs = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0]
        if guess:
            diffs = [d for d in diffs if 0.6 * guess <= d <= 1.4 * guess]
        if diffs:
            d = sorted(diffs)[len(diffs) // 2]
            self._h_est = d if self._h_est is None else 0.7 * self._h_est + 0.3 * d
            return self._h_est
        return guess or 1.0

    def update(self, lines) -> list[str]:
        cur: list[_Line] = []
        boxes: list[float] = []
        for text, y, bh in self._coerce(lines):
            if (msg := parse_message(text)) is not None:
                cur.append(_Line(text, msg, y))
                if bh:
                    boxes.append(bh)
        mem = self.mem
        h = self._pitch(cur, boxes)
        tol = 0.35 * h

        best_k, best_score, best_pairs = len(cur), -math.inf, {}
        for k in range(len(cur) + 1):
            # The bottom k slots hold new lines: everything at or below this y.
            new_from = (cur[-1].y - (k - 0.5) * h) if k and cur else math.inf
            score = -0.1 * sum(l.y > new_from for l in cur)  # prefer fewer new lines
            pairs: dict[int, int] = {}  # cur index -> mem index
            for mi, m in enumerate(mem):
                want = m.y - k * h
                near = [ci for ci, c in enumerate(cur) if abs(c.y - want) <= tol and ci not in pairs]
                if not near:
                    continue  # nothing read there: faded, occluded or misread
                ci = min(near, key=lambda ci: abs(cur[ci].y - want))
                if cur[ci].y > new_from:
                    score -= 1  # an old line can't sit in a new slot
                    continue
                # Same: +1. Garbled item name: 0. A different message where
                # this one should be: -1 (counted once, not also unexplained).
                score += _likeness(m.msg, cur[ci].msg)
                pairs[ci] = mi
            # Old-slot lines we have never seen are unexplained.
            score -= sum(1 for ci, c in enumerate(cur) if c.y <= new_from and ci not in pairs)
            if score > best_score:
                best_k, best_score, best_pairs = k, score, pairs

        new_from = (cur[-1].y - (best_k - 0.5) * h) if best_k and cur else math.inf
        new = [c for c in cur if c.y > new_from]
        if not mem:
            new = cur  # first read: everything on screen is new to us
        # Memory: lines read now, plus recently seen ones that weren't read this
        # time (shifted along), so a briefly hidden line isn't counted again.
        seen = set(best_pairs.values())
        kept = [
            _Line(m.text, m.msg, m.y - best_k * h, m.unseen + 1)
            for mi, m in enumerate(mem)
            if mi not in seen and m.unseen + 1 <= self.keep and m.y - best_k * h > -h
        ]
        # Drop remembered lines whose slot is now occupied by a read line.
        kept = [m for m in kept if all(abs(m.y - c.y) > tol for c in cur)]
        self.mem = sorted(cur + kept, key=lambda l: l.y)
        return [c.text for c in new]


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
    # Not in a party: every EXP line is your own kill at the solo value, so
    # party flags (share scaling, ambiguity) don't apply.
    solo: bool = False

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
            self.party.append(party and not self.solo)
            self._dirty = True

    def mark_last_party(self) -> None:
        if self.party and not self.solo:
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

    def counts(self, limit: int | None = None) -> Counter[int | None]:
        """Kills per mob id for this map segment (None = unidentified).

        ``limit`` caps the total (see ``Session.total_kills``): counts are
        scaled down proportionally, largest remainders first.
        """
        self._fit()
        trust_party = self.solo or self.party_identifiable()
        c = Counter(
            (m.id if (not p or trust_party) and (m := self.classify(v)) else None)
            for v, p in zip(self.values, self.party)
        )
        total = sum(c.values())
        if limit is None or limit >= total:
            return c
        shares = {k: n * limit / total for k, n in c.items()}
        out = Counter({k: int(v) for k, v in shares.items()})
        for k in sorted(shares, key=lambda k: shares[k] - out[k], reverse=True)[: limit - sum(out.values())]:
            out[k] += 1
        return +out


def coverage(message_sum: int, bar_delta: int) -> float | None:
    """Fraction of EXP-bar gain explained by messages we read (1.0 = nothing missed)."""
    if bar_delta <= 0:
        return None
    return message_sum / bar_delta


def credited_kills(values: Sequence[int], bar_gain: int, verified: int, allowance: int = 0) -> int:
    """How many kills the EXP bar can account for.

    ``values`` are the "You received EXP" amounts in order; the first
    ``verified`` were read before the latest EXP-bar reading, so the bar should
    already include them (later ones are still in flight and are allowed).
    Their sum can't exceed what the bar gained (plus ``allowance`` for kills
    made before the bar had a baseline), so any excess was read twice.
    """
    budget = bar_gain + allowance
    total = n = 0
    for v in values[:verified]:
        if total + v > budget:
            break
        total += v
        n += 1
    return n + max(0, len(values) - verified)


def estimate_missed(message_sum: int, bar_delta: int, kills: int) -> int:
    """Kills probably missed (lines scrolled away / ambiguous duplicates)."""
    if kills == 0 or bar_delta <= message_sum:
        return 0
    avg = message_sum / kills
    return round((bar_delta - message_sum) / avg)
