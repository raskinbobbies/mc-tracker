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
# Party system messages in the chat box, each matched as the whole line (after
# its "[19:12]" timestamp) so player chat quoting one never counts.
# A leading timestamp, OCR-tolerant: "[18:08]", "(19:12)", "19:12]", "[18.08".
CHAT_TIME_RE = re.compile(r"^\s*[^\w\s]?\s*\d{1,2}\s*[:.;,]\s*\d{2}\s*[^\w\s'\"‘’`]?\s*")
PLAYER_CHAT_RE = re.compile(r"^[^:]{1,30}:")  # "name : text" or "name: text"
# Whispers ("Whisperer>> text") and notices ("[Notice] text") are never party events.
WHISPER_RE = re.compile(r">>|<<")
NOTICE_RE = re.compile(r"^[\[\(\{]\s*notice", re.IGNORECASE)
# Party system lines are drawn in the same salmon as other system messages;
# chat is white, buddy orange, party chat pink-purple, whispers green, notices
# yellow (``vision.reader.chat_row_colour`` classifies a chat row).
PARTY_EVENT_COLOUR = "salmon"
_Q = "['\"‘’`]?"  # names are quoted: "'Ahbabo' has joined the party."
_NAME = rf"{_Q}(?P<who>[A-Za-z0-9]{{2,13}}){_Q}"
_END = r"\s*[.!]?\s*$"
PARTY_EVENT_RES = tuple((event, re.compile(rf"^{rx}{_END}", re.IGNORECASE)) for event, rx in (
    ("created", r"you\s*have\s*created\s*a\s*new\s*party"),
    ("disbanded", r"the\s*party\s*has\s*been\s*disbanded"),
    ("joined", r"(?P<who>you)\s*have\s*joined\s*the\s*party"),
    ("joined", rf"{_NAME}\s*has\s*joined\s*the\s*party"),
    ("left", r"(?P<who>you)\s*have\s*left\s*the\s*party"),
    ("left", r"(?P<who>you)\s*have\s*been\s*expelled\s*from\s*the\s*party"),  # = you left
    ("left", rf"{_NAME}\s*has\s*left\s*the\s*party"),
    ("left", rf"{_NAME}\s*(?:has\s*)?been\s*expelled\s*from\s*the\s*party"),
))


def _int(s: str) -> int | None:
    d = re.sub(r"\D", "", s.replace("l", "1").replace("I", "1").replace("|", "1"))
    return int(d) if d else None


@dataclass(frozen=True)
class Message:
    kind: str  # "exp" | "party_bonus" | "loot" | "mesos" | "party"
    value: int = 0  # EXP amount, loot quantity or mesos
    item: str = ""  # loot: item name; party: who ("you" or a name)
    category: str = ""  # loot: tab; party: created | joined | left | disbanded


def colour_allows_party(colour: str | None) -> bool:
    """Can a chat row of this text colour be a party event? An unknown colour
    (None: not read, or unclear) never hides an event from the text rules."""
    return colour is None or colour == PARTY_EVENT_COLOUR


def parse_message(text: str, colour: str | None = None) -> Message | None:
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
    # Chat box lines start with a "[19:12] " timestamp. Player chat of any kind
    # ("name : text", buddy, party, whispers "name>> text") and notices are never
    # party events, whatever they say; neither is a row in a known non-salmon colour.
    if not colour_allows_party(colour):
        return None
    text = CHAT_TIME_RE.sub("", text, count=1).strip()
    if PLAYER_CHAT_RE.match(text) or WHISPER_RE.search(text) or NOTICE_RE.match(text):
        return None
    for event, rx in PARTY_EVENT_RES:
        if m := rx.match(text):
            who = m.groupdict().get("who") or "you"
            return Message("party", 0, "you" if who.lower() == "you" else who, event)
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


def visible_rows(lines) -> list[tuple[str, float, float]]:
    """Visible lines as (text, y, height), top (oldest) to bottom (newest),
    same-row OCR fragments joined. Lines are TextLine-like (text, y, height,
    x0), (text, y) tuples or plain strings (already in order, one row apart)."""
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
    return [(" ".join(t[0] for t in sorted(row, key=lambda t: t[3])), row[0][1], row[0][2])
            for row in rows]


def visible_texts(lines) -> list[str]:
    """Texts of ``visible_rows``."""
    return [r[0] for r in visible_rows(lines)]


HIDDEN = 2  # LineDiff: a message unseen since the area went empty


class LineDiff:
    """New messages in a scrolling message area, by position.

    New messages come in at the bottom and push the older ones up a row. Each
    message read gets a *slot*: its y plus how far the area has scrolled
    since, which stays put while it scrolls. Each frame, the scroll is the
    shift that lines up the most visible messages with remembered ones (same
    text, within half a row); ties go to the shift that leaves no unlined
    message above every slot shown lately (new messages come in at the
    bottom), then the smallest. A
    message in a slot nobody had is new; one in a taken slot is a re-read
    (hidden for a while, or misread once). So a repeated message (the same
    EXP twice) counts once per copy even when an identical copy scrolls off as
    it arrives, and one hidden for a few frames isn't counted again.

    Slots are kept ``keep_items`` frames after last seen. A message missing
    with nothing shown above it has faded or scrolled off (they fade oldest
    first): it lines up the scroll only for ``keep`` frames more, so a new
    copy shown in its place after a fade is new. After 3 frames with no
    messages (faded, or the reads failed) the messages hidden then line up
    only if ``rejoin`` or more do, or if they were first read under
    ``young_s`` ago (a message stays up longer than that, so they can't have
    faded): a full read after failed fast reads shows them again, while a
    new message or a kill and its bonus after a fade can't line up.
    When nothing lines up (the area faded), the visible messages are new and take over their slots;
    as the scroll then is unknown, for ``keep`` frames they line up anywhere. A full
    area of the very same message can't show a scroll: it undercounts. Kills
    never depend on lines alone: they come from the EXP bar (see
    ``BarKills``). Item names are matched loosely (OCR garbles them, see
    ``_same``).

    ``update`` returns the new messages in screen order (oldest first).
    """

    def __init__(self, keep: int = 4, keep_items: int = 30, rejoin: int = 3, young_s: float = 6.0) -> None:
        self.keep = keep
        self.rejoin = rejoin
        self.young_s = young_s
        self.keep_items = keep_items
        self._frame = 0
        self._scroll = 0.0  # how far the area has scrolled up since the start
        self._empty = 0  # frames in a row with no messages
        self._bottom = -math.inf  # the lowest row seen: where new messages come in
        # [message, slot, last frame seen, gone (True: from the top; HIDDEN: in empty frames),
        #  placed unaligned, first read at (``now``)]
        self._seen: list[list] = []
        self.shown: set[str] = set()  # kinds of the messages in the last frame (new or not)

    def update(self, lines, now: float | None = None) -> list[Message]:
        self._frame += 1
        f, s = self._frame, self._scroll
        self._seen = [e for e in self._seen if f - e[2] <= self.keep_items]
        rows = [(m, r[1], r[2]) for r in visible_rows(lines) if (m := parse_message(r[0]))]
        self.shown = {m.kind for m, _, _ in rows}
        emptied, self._empty = self._empty >= 3, 0 if rows else self._empty + 1
        if not rows:
            if self._empty >= 3:  # an empty frame or two may be bad reads; three, it faded
                for e in self._seen:
                    e[3] = e[3] or HIDDEN
            return []
        hs = sorted(h for _, _, h in rows if h)
        gaps = [b[1] - a[1] for a, b in zip(rows, rows[1:]) if b[1] > a[1]]
        tol = 0.45 * (hs[len(hs) // 2] if hs else min(gaps, default=1.0))
        live = [e for e in self._seen if not e[3] or f - e[2] <= self.keep or emptied and e[3] is HIDDEN]
        cands = [[e for e in live if _same(m, e[0])] for m, _, _ in rows]
        best = (-math.inf,)
        top = min((e[1] for e in self._seen if f - e[2] <= self.keep), default=math.inf) - tol
        for d in {0.0} | {round(e[1] - s - y, 1) for (_, y, _), es in zip(rows, cands) for e in es}:
            if d < -tol:
                continue  # messages never scroll down
            used, match = set(), []
            for (_, y, _), es in zip(rows, cands):
                e = min((e for e in es if id(e) not in used and abs(e[1] - s - d - y) <= tol),
                        key=lambda e: abs(e[1] - s - d - y), default=None)
                if e is not None:
                    used.add(id(e))
                match.append(e)
            above = sum(1 for (_, y, _), e in zip(rows, match) if e is None and y + s + d < top)
            best = max(best, (len(used), -above, -abs(d), d, match), key=lambda k: k[:3])
        for e in self._seen:
            if e[3] is HIDDEN:
                e[3] = True
        if emptied and best[0] and (best[0] >= self.rejoin or now is not None and all(
                now - e[5] < self.young_s for e in best[4] if e is not None and e[5] is not None)):
            emptied = False  # the hidden messages again, not new ones
        if best[0] and not emptied:
            self._scroll = s = s + best[3]
            match = best[4]
        else:  # nothing lines up (or the area was empty): what was shown faded
            best, match = (0,), [None] * len(rows)
        self._bottom = max(self._bottom, rows[-1][1])
        out: list[Message] = []
        for (m, y, _), e in zip(rows, match):
            if e is None:  # placed when nothing lined up, so maybe before a scroll: anywhere
                e = next((x for x in self._seen if x[4] and f - x[4] <= self.keep
                          and x[2] != f and _same(m, x[0])), None)
            if e is None and not emptied:  # a taken slot: a re-read, maybe misread
                taken = sorted((x for x in self._seen if abs(x[1] - y - s) <= tol),
                               key=lambda x: not _same(m, x[0]))
                e = taken[0] if taken else None
                if e is not None and not best[0] and y > self._bottom - tol and (
                        e[3] or f - e[2] > self.keep):
                    e = None  # a new message where a faded one was
                elif e is not None and not (_same(m, e[0]) or m.item and e[0].kind == m.kind):
                    if not best[0]:
                        e = None  # nothing lines up: not a misread, a new message
                    else:
                        continue
            if e is not None:
                if e[4] and best[0]:  # placed unaligned: its frame-mates move with it
                    for x in self._seen:
                        if x[4] == e[4] and x is not e:
                            x[1], x[4] = x[1] + y + s - e[1], 0
                    e[4] = 0
                e[1], e[2], e[3] = y + s, f, False
                continue
            for x in self._seen:  # the slot's old message is gone
                if abs(x[1] - y - s) <= tol:
                    x[3] = True
            self._seen.append([m, y + s, f, False, 0 if best[0] else f, now])
            out.append(m)
        top = rows[0][1] + s + tol
        for e in self._seen:  # gone with nothing shown above it: faded or scrolled off
            if e[2] != f and e[1] < top:
                e[3] = True
        return out


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
    if it falls, new kills wait until it passes its old high). Gain with no
    EXP line read near it in time (quests, pets, anything not a kill) is
    quarantined. Every doubt rounds down.

    Solo, the pool must be a sum of known per-kill values (fewest kills wins)
    needing at most one kill more than new lines were read (and it must match
    a value read lately); otherwise only the lines it covers count and the
    rest is quarantined.

    In a party, kills are counted per *epoch* (a stretch with the same party,
    share and level): ``lines + (G - paid) // U``. ``lines`` are the epoch's
    EXP lines paid for by bar gain within ``nearby_s`` of them (``paid``: each
    one's share plus its bonus line, or the bonus it would have had); ``G``
    is the epoch's kill gain and
    ``U`` the largest clean kill + bonus pair seen in it. Each kill whose line
    was missed is worth at most ``U``, so the count can't pass the real one,
    and a misread or a dropped bonus line only lowers it. Lines alone miss
    kills when every kill gives the same EXP (a message area full of
    identical lines shows no scroll); the bar doesn't. The ``// U`` part waits
    for ``min_pairs`` pairs and counts gain only ``lag_s`` old, so a larger
    pair seen late lowers it before it shows. A pair above ``U`` (the share grew),
    a party size change or a level-up starts a new epoch: ``U`` may be stale,
    and gain spanning a level-up wrap is dropped unless the lines waiting for
    it explain it exactly.
    """

    nearby_s: float = 8.0  # an EXP line this close to a gain makes it kill EXP
    stale_s: float = 10.0  # solo: pool unconverted this long -> quarantined
    tol: int = 2  # EXP a sum may fall short by (rounding)
    max_parts: int = 20  # solo: a pool above this many of the largest value isn't decomposed
    recent_s: float = 30.0  # solo: a kill with no line must match a line value read this recently
    repeat_s: float = 8.0  # party: a line without its bonus repeating a value this recent may be a re-read
    flip_s: float = 2.0  # solo -> party: a pool this fresh is kept (its bonus line came just after)
    lag_s: float = 12.0  # party: bar gain this old counts toward G // U
    min_pairs: int = 3  # party: pairs an epoch needs before G // U counts
    total: int = 0
    quarantined: int = 0  # bar EXP not converted to kills
    per_kill: deque = field(default_factory=lambda: deque(maxlen=50))  # party: kill + bonus pair totals
    counted: list = field(default_factory=list)  # (line value, party) of each line counted as a kill
    _high: int = 0
    _pool: int = 0
    _pool_t: float | None = None
    _line_t: float | None = None
    _lines: list = field(default_factory=list)  # uncounted lines: [time, value, EXP with bonus, bonus seen]
    _party: bool = False
    _cache: tuple = ((), (0, 0))  # last decompose: (pool, values) -> result
    _seen: deque = field(default_factory=deque)  # (time, value) of recent lines
    clean: Counter = field(default_factory=Counter)  # solo: value -> times the gain was exactly one line's EXP
    _bonuses: dict = field(default_factory=dict)  # party: kill line value -> its bonus (last pair)
    _ratio: deque = field(default_factory=lambda: deque(maxlen=20))  # party: bonus / kill of recent pairs
    # Party: gain waiting for a line near it [time, EXP]. The epoch: its kill
    # gain under ``lag_s`` old [time, EXP no line was paid from], older such
    # gain (settled), largest pair, pairs, lines paid, kills counted.
    _shown_t: float = -math.inf  # party: last frame showing an EXP line (new or not)
    _wait: list = field(default_factory=list)
    _gain: list = field(default_factory=list)
    _settled: int = 0
    _U: int = 0
    _pairs: int = 0
    _paid: int = 0
    _done: int = 0

    def line(self, now: float, value: int = 0) -> None:
        """A new EXP line was read."""
        self._line_t = now
        self._lines.append([now, value, value, False])
        self._seen.append((now, value))

    def shown(self, now: float) -> None:
        """An EXP line is on screen, new or not: kill gain near it is a kill's
        (a message area full of the same line shows nothing new for a while)."""
        self._shown_t = now

    def bonus(self, value: int) -> None:
        """A party bonus line: it belongs to the line just before it."""
        if self._lines and not self._lines[-1][3]:
            self._lines[-1][2] += value
            self._lines[-1][3] = True

    def pair(self, total: int, bonus: int = 0) -> None:
        """A clean kill + party bonus pair. When the recent pairs move well away
        from the older ones (someone joined or left), the older ones go."""
        if 0 < bonus < total:
            self._bonuses[total - bonus] = bonus
            self._ratio.append(bonus / (total - bonus))
        self.per_kill.append(total)
        old, new = list(self.per_kill)[:-5], list(self.per_kill)[-5:]
        if len(old) >= 5 and not 0.67 <= _median(new) / _median(old) <= 1.5:
            self.per_kill = deque(new, maxlen=self.per_kill.maxlen)
        if self._pairs >= self.min_pairs and total > self._U + self.tol:
            self.boundary()  # a larger share: the epoch's U may be stale
        self._U, self._pairs = max(self._U, total), self._pairs + 1

    def boundary(self) -> None:
        """Start a new party epoch. What the old one counted stays; its gain
        no line was paid from and ``G // U`` hasn't counted yet is dropped."""
        self.quarantined += sum(g for _, g in self._gain)
        self._gain = []
        self._settled = self._U = self._pairs = self._paid = self._done = 0

    def _cost(self, ln: list) -> int:
        """A party line's EXP: with its bonus line, or the bonus it would have.
        One without a bonus line repeating a value just read may be that line
        read again: it costs the most a kill is worth (``U``), so it adds
        nothing over ``// U``."""
        if ln[3]:
            return ln[2]
        b = self._bonuses.get(ln[1])
        est = ln[1] + (b if b is not None else math.ceil(ln[1] * max(self._ratio, default=0)))
        if self._pairs >= self.min_pairs and any(
                v == ln[1] and 0 < ln[0] - t <= self.repeat_s for t, v in self._seen):
            return max(est, self._U)
        return est

    def update(self, gain: int, now: float, solo_values: Sequence[int] = (), party: bool = False,
               hold: bool = False, wrap: bool = False) -> int:
        """Book new bar gain; -> kills added. ``hold``: the bar is unsure,
        wait. ``wrap``: this gain spans a level-up."""
        if party != self._party:  # the pool was gathered under the other rule
            self._party = party
            if party and self._pool_t is not None and now - self._pool_t <= self.flip_s:
                # A first kill after a pause reads as solo until its bonus line
                # shows: a pool that fresh is the party kill's, not a solo one's.
                self._wait.append([self._pool_t, self._pool])
                self._pool = 0
            elif not party:
                self.boundary()
                self.quarantined += sum(w[1] for w in self._wait)
                self._wait = []
            self._quarantine()
        new = max(0, gain - self._high)
        self._high = max(self._high, gain)
        self._lines = [ln for ln in self._lines if now - ln[0] <= self.nearby_s]
        while self._seen and now - self._seen[0][0] > self.recent_s:
            self._seen.popleft()
        if party:
            return self._party_update(new, now, hold, wrap)
        if new:
            self._pool += new
            if self._pool_t is None:
                self._pool_t = now
        if self._pool <= 0 or hold or self._pool_t is None:
            return 0
        if self._line_t is None or self._line_t < self._pool_t - self.nearby_s:
            if now - self._pool_t > self.nearby_s:
                self._quarantine()  # no kill line anywhere near it
            return 0
        n = self._solo_kills(solo_values)
        if not n and now - self._pool_t > self.stale_s:
            # No sum in time (values not all trusted yet): just the lines it covers.
            n = self._covered(tuple(v for v in solo_values if v > 0))
            self._take(n, False)
            self._pool = 0
        if n:
            self.total += n
            self._pool_t = now if self._pool > 0 else None
        elif now - self._pool_t > self.stale_s:
            self._quarantine()
        return n

    def _take(self, n: int, party: bool) -> None:
        self.counted.extend((ln[1], party) for ln in self._lines[:n])
        del self._lines[:n]

    def _solo_kills(self, values: Sequence[int]) -> int:
        if len(self._lines) == 1 and self._pool == self._lines[0][1] > 0:
            self.clean[self._pool] += 1  # one line, gain exactly its EXP: a per-kill value
        vals = tuple(sorted({v for v in values if v > 0}))
        if not vals:
            return 0
        lines = len(self._lines)
        rest = self._pool - sum(ln[1] for ln in self._lines)
        if all(ln[1] in vals for ln in self._lines):  # trusted lines: their EXP, maybe one kill more
            if lines and 0 <= rest <= self.tol:
                self._take(lines, False)
                self._pool = 0
                return lines
            if any(abs(rest - v) <= self.tol for _, v in self._seen if v in vals):
                self._take(lines, False)  # plus one kill whose line was missed
                self._pool = 0
                return lines + 1
        n = lines + 1  # too big to decompose
        if self._pool <= self.max_parts * vals[-1]:
            key = (self._pool, vals)
            if self._cache[0] != key:
                self._cache = (key, decompose(self._pool, vals, self.tol))
            n, used = self._cache[1]
        if n > lines:
            n = self._covered(vals)  # the rest (a quest...) is quarantined
            used = self._pool
        self._take(n, False)
        self._pool -= used
        return n

    def _covered(self, vals: Sequence[int]) -> int:
        """Solo: the trusted lines the pool covers; the rest is quarantined."""
        n = used = 0
        for ln in self._lines:
            if ln[1] not in vals or used + ln[1] > self._pool:
                break
            n, used = n + 1, used + ln[1]
        self.quarantined += self._pool - used
        self._pool = used
        return n

    def _party_update(self, new: int, now: float, hold: bool, wrap: bool) -> int:
        if wrap and new:
            # Gain over a level-up leans on the EXP table: keep it only if the
            # lines waiting for the bar add up to it exactly.
            acc = 0
            for ln in self._lines:
                acc += ln[2]
                if abs(acc - new) <= self.tol:
                    break
            else:
                self.quarantined += new
                new = 0
            self.boundary()
        if new:
            self._wait.append([now, new])
        if hold:
            return 0
        for w in self._wait:  # kill gain if an EXP line was read near it
            if max(self._line_t or -math.inf, self._shown_t) >= w[0] - self.nearby_s:
                self._gain.append([w[0], w[1]])
                w[1] = 0
            elif now - w[0] > self.nearby_s:
                self.quarantined += w[1]  # no kill line anywhere near it
                w[1] = 0
        self._wait = [w for w in self._wait if w[1]]
        while self._gain and now - self._gain[0][0] >= self.lag_s:  # no line pays it any more
            self._settled += self._gain.pop(0)[1]
        took = []
        for ln in self._lines:  # each line's EXP must be in the gain near it
            near = [g for g in self._gain if abs(g[0] - ln[0]) <= self.nearby_s and g[1]]
            if (c := self._cost(ln)) <= sum(g[1] for g in near) + self.tol:
                took.append(ln)
                for g in near:
                    c -= (d := min(c, g[1]))
                    g[1] -= d
        if took:
            self._lines = [ln for ln in self._lines if all(ln is not x for x in took)]
            self.counted.extend((ln[1], True) for ln in took)
            self._paid += len(took)
        n = self._paid
        if self._pairs >= self.min_pairs:  # kills whose lines were missed: at most U each
            n += self._settled // self._U
        add = max(0, n - self._done)
        self._done += add
        self.total += add
        return add

    def _quarantine(self) -> None:
        self.quarantined += self._pool
        self._pool, self._pool_t = 0, None


# The top-mob window never starts below ``second.exp * E_MAX``. This is a margin both
# against EXP multipliers (event, coupon) we don't know about and against misread
# shares: the owner reports no multipliers so far, but at 1.0 a 3% misread rate
# overcounts in ~3% of runs (vs ~0.03% at 2.0). Don't lower it without re-running a
# misread probe (#27).
E_MAX = 2.0


def _median(xs) -> float:
    s = sorted(xs)
    return (s[len(s) // 2] + s[(len(s) - 1) // 2]) / 2


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
    _assign: list = field(default_factory=list, init=False)  # [(value, mob)] behind ``scale``
    _solo_lines: int = field(default=0, init=False)  # solo lines behind ``scale``
    _solo_ok: set | None = field(default=None, init=False)  # trusted solo values (None: all)
    _solo_trusted: bool = field(default=False, init=False)  # ``scale`` is a trusted solo fit
    _over_top: bool = field(default=False, init=False)  # a line above the top mob's value
    _dirty: bool = field(default=False, init=False)

    def set_map(self, mobs: Sequence[Mob]) -> None:
        """Start a new map segment (counts so far are kept by the caller)."""
        self.mobs = [m for m in mobs if m.exp > 0]
        self.values.clear()
        self.party.clear()
        self.scale = 1.0
        self._assign, self._solo_lines, self._solo_ok = [], 0, None
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

    @property
    def total(self) -> int:
        return len(self.values)

    # Solo EXP from one monster type is the same number every kill, so values
    # within this relative tolerance are treated as one monster type.
    cluster_tol: float = 0.04
    # Pull toward 1x scale (no event / no level penalty) unless data says otherwise.
    prior_weight: float = 0.02
    # Solo lines (in at least 2 distinct monster clusters) needed before their fitted
    # scale is trusted in a party: one or two strays, or one monster's kills alone, can
    # fit any scale.
    min_solo_fit: int = 3

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
        solo = [v for v, p in zip(self.values, self.party) if not p]
        self._solo_ok = None
        if any(self.party):
            # In a party, a line flagged solo may be a share whose bonus line was missed,
            # and one such line skews the fit for every other solo line. A real solo kill
            # gives the same number every time, so only values seen twice are trusted.
            n = Counter(solo)
            self._solo_ok = {v for v, c in n.items() if c >= 2}
            solo = [v for v in solo if v in self._solo_ok]
        self._solo_lines = len(solo)
        self.scale, self._assign = self._fit_obs((solo or self.values)[-self.fit_window:], self.scale)
        # One cluster can't pin the scale (the prior picks whichever monster is nearest
        # 1x), so a solo scale is trusted only when 2+ distinct clusters agree on it.
        self._solo_trusted = (
            bool(solo) and self._solo_lines >= self.min_solo_fit and len(self._assign) >= 2
            and self._assign_sound(solo[-self.fit_window:])
        )
        if self._solo_ok is not None and not self._solo_trusted:
            self._solo_ok = set()  # in a party, an untrusted solo fit classifies nothing
        # A line worth more than the top mob's full value means a multiplier above what
        # the window assumes (or a garbled read): count no party share in this segment.
        top = max((m.exp for m in self.mobs), default=0)
        cap = top * (self.scale if self._solo_trusted else 1.0) * (1 + self.cluster_tol)
        self._over_top = bool(top) and any(v > cap for v in self.values)

    def _assign_sound(self, obs: list[int]) -> bool:
        """The solo fit's cluster -> mob assignment can be trusted: no cluster dropped,
        each cluster within ``cluster_tol`` of its mob at the fitted scale, and no mob
        skipped between the lowest and highest assigned (shares at a steady ratio,
        their bonus lines missed, can otherwise fit a scale that shifts every cluster
        onto a higher mob)."""
        if len(self._assign) != len(self._clusters(obs)):
            return False
        tol = math.log(1 + self.cluster_tol)
        if any(abs(math.log(v / (m.exp * self.scale))) > tol for v, m in self._assign):
            return False
        ranks = sorted({m.exp for m in self.mobs})
        idx = sorted(ranks.index(m.exp) for _, m in self._assign)
        return idx == list(range(idx[0], idx[0] + len(idx)))

    def _fit_obs(self, obs: list[int], scale: float) -> tuple[float, list[tuple[float, Mob]]]:
        """(scale, [(value, mob)]) that best map the observed EXP values onto the monsters."""
        mobs = sorted({m.exp: m for m in self.mobs}.values(), key=lambda m: m.exp)
        if not obs or not mobs:
            return scale, []
        clusters = self._clusters(obs)
        if len(clusters) > len(mobs) or len(clusters) > 8:
            return self._fit_scale_only(obs), []
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
            return math.exp(lk), [(v, m) for (v, _), m in zip(clusters, combo)]
        return scale, []

    def _fit_scale_only(self, obs: list[int]) -> float:
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
        return math.exp(best)

    def classify(self, exp_value: int) -> Mob | None:
        """The monster a full-value (solo) EXP line is closest to. Never call it on a
        party share: a share is a fraction of the kill, so it would match a lower mob."""
        self._fit()
        if not self.mobs or exp_value <= 0:
            return None
        scale, assign = self.scale, self._assign
        # Two monsters within 2 * cluster_tol of the value at this scale (e.g. 130 vs
        # 135): their kills merge into one cluster, so the line can't be told apart.
        near = [m for m in self.mobs if abs(exp_value - m.exp * scale) <= 2 * self.cluster_tol * m.exp * scale]
        if len(near) >= 2:
            return None
        for v, mob in assign:
            if abs(exp_value - v) <= v * self.cluster_tol:
                return mob
        lo = math.log(exp_value) - math.log(scale)
        mob = min(self.mobs, key=lambda m: abs(lo - math.log(m.exp)))
        return mob if abs(lo - math.log(mob.exp)) <= self.max_log_error else None

    def identify(self, exp_value: int, party: bool = False) -> int | None:
        """The mob id an EXP line came from, or None if unsure (undercount only).

        A line flagged solo is your own kill at full value and is classified mob by mob.
        A party share is some unknown fraction of the kill's EXP, so it is never
        classified mob by mob: it only counts for the top mob, when it is worth more than
        any other mob could give (``lo < v <= hi``), and is otherwise left unassigned.
        """
        # Hook for #30: once the EXP line's colour is read, a white line (your own
        # kill, full value) arrives here with party=False and is classified at full
        # value; a yellow line (party share) arrives with party=True. White lines are
        # then known solo kills and can skip the repeat check (``_solo_ok``), but even
        # they must pass the 2-cluster check (``_solo_trusted``) before their scale is
        # used, and a solo scale may only raise the window's ``lo``, never lower it.
        self._fit()
        if self.solo or not party:
            if self._solo_ok is not None and exp_value not in self._solo_ok:
                return None  # a one-off "solo" line in a party: maybe a misread share
            return m.id if (m := self.classify(exp_value)) else None
        top = sorted(self.mobs, key=lambda m: m.exp)[-2:]
        if not top or self._over_top:
            return None
        # ``lo``: above any share the second mob could give, even under an EXP event
        # (a trusted solo scale can only raise it). ``hi``: the top mob's full value, at
        # 1x unless a trusted solo scale says otherwise. ``lo >= hi``: nothing counts.
        trusted = self.scale if self._solo_trusted else None
        tol = 1 + self.cluster_tol
        hi = top[-1].exp * (trusted or 1.0) * tol
        lo = top[0].exp * max(E_MAX, trusted or 0.0) * tol if len(top) > 1 else 0
        return top[-1].id if lo < exp_value <= hi else None

    def counts(self) -> Counter[int | None]:
        """Lines per mob id for this map segment (None = unidentified)."""
        return Counter(self.identify(v, p) for v, p in zip(self.values, self.party))


def split(counts: Counter, total: int) -> Counter:
    """``total`` kills split by each key's share of ``counts``, rounded down;
    the rest (and the share of None, unidentified lines) goes to None."""
    n = sum(counts.values())
    out = Counter({k: total * c // n for k, c in counts.items() if k is not None} if n else {})
    out = +out
    if total - sum(out.values()):
        out[None] = total - sum(out.values())
    return out

