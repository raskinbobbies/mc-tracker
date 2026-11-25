"""Party kills from the EXP bar, split per monster by the EXP messages.

Every monster your party kills on your map gives you a message, whoever
killed it (confirmed in game):

    You received EXP (+39)
    Bonus EXP for PARTY (+4)      <- only when in a party

Counting those lines over-counts: the same line is re-read as the message box
scrolls and fades (2 kills once showed as 9). So the EXP bar gives the
magnitude and the messages give the mix (``BarKills``): kills are converted
from bar gain only, and each monster gets its share of the sampled lines
(``KillCounter.split``), rounded down; the rest is "unassigned".

A line's monster is found by matching its EXP against the current map's
monsters. Solo, the value equals the monster's EXP (times any event bonus), so
we fit one multiplicative scale ``k`` and assign each value to the nearest
``k * base_exp`` in log space. In a party your share varies per kill (damage
split), so party kills are only identified when the map's monsters are far
enough apart in EXP.
"""

from __future__ import annotations

import math
import re
from itertools import combinations
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Sequence

from rapidfuzz import fuzz

EXP_RE = re.compile(r"received\s*EXP\s*\(\s*\+\s*([\d,.]+)", re.IGNORECASE)
PARTY_BONUS_RE = re.compile(r"Bonus\s*EXP\s*for\s*PARTY\s*\(\s*\+\s*([\d,.]+)", re.IGNORECASE)
# "You have gained mesos (+10)"; OCR may drop spaces or read 's' as '5'.
MESOS_RE = re.compile(r"gained\s*m[e3][s5][o0][s5]?\s*\(?\s*\+\s*(\d[\d,.\s]*)", re.IGNORECASE)
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
    kind: str  # "exp" | "party_bonus" | "loot" | "mesos" | "party"
    value: int = 0  # EXP amount, loot quantity or mesos
    item: str = ""  # loot: item name; party: who ("you" or a name)
    category: str = ""  # loot: tab; party: created | joined | left | disbanded


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


def _same(a: Message, b: Message) -> bool:
    """Two reads of the same on-screen message? Numbers must match exactly;
    item names may differ by OCR noise ("Pumpkin" / "Purpkin" / "Piece") and
    the loot tab is ignored ("Etc" / "Eic")."""
    if (a.kind, a.value) != (b.kind, b.value) or (a.kind == "party" and a.category != b.category):
        return False
    if not (a.item or b.item):
        return a.category == b.category
    x, y = _norm(a.item), _norm(b.item)
    return x == y or (bool(x and y) and fuzz.partial_ratio(x, y) >= 70)


def visible_texts(lines) -> list[str]:
    """Visible lines, top (oldest) to bottom (newest), same-row OCR fragments
    joined. Lines are TextLine-like (text, y, height, x0), (text, y) tuples or
    plain strings (already in order)."""
    raw = []
    for i, l in enumerate(lines):
        if isinstance(l, str):
            raw.append((l, float(i), 0.0, 0.0))
        elif isinstance(l, tuple):
            raw.append((l[0], float(l[1]), 0.0, 0.0))
        else:
            raw.append((l.text, float(l.y), float(getattr(l, "height", 0) or 0),
                        float(getattr(l, "x0", 0) or 0)))
    raw.sort(key=lambda t: t[1])
    rows: list[list[tuple[str, float, float, float]]] = []
    for r in raw:
        # Same row: OCR sometimes splits one line. A much taller box on the
        # same row is a damage number, not a fragment.
        p = rows[-1][0] if rows else None
        if (p and r[2] and p[2] and abs(r[1] - p[1]) < 0.3 * min(r[2], p[2])
                and max(r[2], p[2]) < 1.8 * min(r[2], p[2])):
            rows[-1].append(r)
        else:
            rows.append([r])
    return [" ".join(t[0] for t in sorted(row, key=lambda t: t[3])) for row in rows]


class LineDiff:
    """New messages in a scrolling message area, by multiset difference.

    The visible messages are compared, as a multiset, with the most any of the
    last ``keep`` frames showed: only the *increase* in the count of each
    distinct message is new. That ignores scrolling, fading, a line hidden for
    a frame or two, and damage numbers floating through (they don't parse).
    Two identical kills in a row show as the count going 1 -> 2. It can miss a
    message (an identical one scrolled off as it arrived) or, rarely, repeat
    one (hidden for longer than ``keep`` frames). Kills never depend on it
    alone: they come from the EXP bar (see ``BarKills``). Messages with an
    item name (loot, party events) are matched loosely (OCR garbles names) and
    against the last ``keep_items`` frames: they undercount rather than repeat.

    ``update`` returns the new messages in screen order (oldest first).
    """

    def __init__(self, keep: int = 4, keep_items: int = 30) -> None:
        self.keep = keep
        self._recent: deque[list[Message]] = deque(maxlen=max(keep, keep_items))

    def update(self, lines) -> list[Message]:
        msgs = [m for t in visible_texts(lines) if (m := parse_message(t))]
        frames = list(self._recent)
        self._recent.append(msgs)
        out: list[Message] = []
        groups: list[list] = []  # [message, new copies left]
        for m in reversed(msgs):  # the newest copies of a message are the new ones
            g = next((g for g in groups if _same(g[0], m)), None)
            if g is None:
                past = frames if m.item else frames[-self.keep:]
                seen = max((sum(_same(m, x) for x in f) for f in past), default=0)
                g = [m, sum(_same(m, x) for x in msgs) - seen]
                groups.append(g)
            if g[1] > 0:
                g[1] -= 1
                out.append(m)
        return out[::-1]


def decompose(total: int, values: Sequence[int], tol: int = 0) -> tuple[int, int]:
    """Fewest kills whose EXP ``values`` sum to ``total`` (or up to ``tol``
    less) -> (kills, EXP used); (0, 0) if no such sum exists."""
    vals = sorted({v for v in values if 0 < v <= total})
    if not vals or total > 200_000:
        return 0, 0
    inf = total + 1
    best = [0] + [inf] * total
    for x in range(1, total + 1):
        best[x] = min((best[x - v] + 1 for v in vals if v <= x), default=inf)
    for used in range(total, max(0, total - tol) - 1, -1):
        if 0 < best[used] < inf:
            return best[used], used
    return 0, 0


@dataclass
class BarKills:
    """Kills from the EXP bar's gain: the bar gives the magnitude, messages the mix.

    ``update`` gets the bar's trusted gain so far (only ever counted upward:
    if it falls, new kills wait until it passes its old high). New gain joins
    a pool, converted to kills only when an EXP line was read near it in time;
    gain with no EXP line nearby (quests, pets, anything not a kill) is
    quarantined. Solo, the pool must be a sum of known per-kill values (fewest
    kills wins); in a party, it's divided by the learned EXP per kill (kill
    plus party-bonus line pairs, with a margin). A pool that fits neither for
    ``stale_s`` is quarantined. Every doubt rounds down.
    """

    nearby_s: float = 8.0  # an EXP line this close to a gain makes it kill EXP
    stale_s: float = 10.0  # pool unconverted this long -> quarantined
    tol: int = 2  # solo: EXP a sum may fall short by (rounding)
    party_margin: float = 1.1  # party: divide by mean EXP per kill x this
    total: int = 0
    quarantined: int = 0  # bar EXP not converted to kills
    per_kill: deque = field(default_factory=lambda: deque(maxlen=50))  # party: EXP per clean kill
    _high: int = 0
    _pool: int = 0
    _pool_t: float | None = None
    _line_t: float | None = None

    def line(self, now: float) -> None:
        """An EXP line was read."""
        self._line_t = now

    def divisor(self) -> float | None:
        return sum(self.per_kill) / len(self.per_kill) * self.party_margin if self.per_kill else None

    def update(self, gain: int, now: float, solo_values: Sequence[int] = (), party: bool = False,
               hold: bool = False) -> int:
        """Book new bar gain; -> kills added. ``hold``: the bar is unsure, wait."""
        if gain > self._high:
            self._pool += gain - self._high
            self._high = gain
            if self._pool_t is None:
                self._pool_t = now
        if self._pool <= 0 or hold or self._pool_t is None:
            return 0
        if self._line_t is None or self._line_t < self._pool_t - self.nearby_s:
            if now - self._pool_t > self.nearby_s:
                self._quarantine()  # no kill line anywhere near it
            return 0
        if party:
            div = self.divisor()
            n = int(self._pool // div) if div else 0
            used = min(self._pool, round(n * div)) if n else 0
        else:
            n, used = decompose(self._pool, solo_values, self.tol)
        if n:
            self.total += n
            self._pool -= used
            self._pool_t = now if self._pool > 0 else None
        elif now - self._pool_t > self.stale_s:
            self._quarantine()
        return n

    def _quarantine(self) -> None:
        self.quarantined += self._pool
        self._pool, self._pool_t = 0, None


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


def split(counts: Counter, total: int) -> Counter:
    """``total`` kills split by each key's share of ``counts``, rounded down;
    the rest (and the share of None, unidentified lines) goes to None."""
    n = sum(counts.values())
    out = Counter({k: total * c // n for k, c in counts.items() if k is not None} if n else {})
    out = +out
    if total - sum(out.values()):
        out[None] = total - sum(out.values())
    return out


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
