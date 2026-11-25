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
from dataclasses import dataclass, field, replace
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


class Doubt:
    """Set by ``LineDiff`` when the slot of a message it returned as new is
    read as another message before this one was read again twice: it may be
    a misread, so it is no trusted kill line (see ``BarKills``)."""

    __slots__ = ("doubted",)

    def __init__(self) -> None:
        self.doubted = False


@dataclass(frozen=True)
class Message:
    kind: str  # "exp" | "party_bonus" | "loot" | "mesos" | "party"
    value: int = 0  # EXP amount, loot quantity or mesos
    item: str = ""  # loot: item name; party: who ("you" or a name)
    category: str = ""  # loot: tab; party: created | joined | left | disbanded
    # Placed late by ``LineDiff``: above a message read before, or as hidden ones rejoined. It
    # may have come in well before it was read, so its time says little about when its kill was.
    late: bool = field(default=False, compare=False)
    # Set later by ``LineDiff`` if the message may have been a misread (see ``Doubt``).
    doubt: Doubt = field(default_factory=Doubt, compare=False, repr=False)


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

    A new message placed above one read before, or new in a frame where messages hidden by 3 or more
    empty frames line up again (the reads failed; it may have come in then), came in before it was
    read, maybe seconds before: it is returned with ``late`` set.

    ``update`` returns the new messages in screen order (oldest first).
    """

    def __init__(self, keep: int = 4, keep_items: int = 30, rejoin: int = 3, young_s: float = 6.0,
                 late_s: float = 1.0) -> None:
        self.keep = keep
        self.late_s = late_s
        self.rejoin = rejoin
        self.young_s = young_s
        self.keep_items = keep_items
        self._frame = 0
        self._scroll = 0.0  # how far the area has scrolled up since the start
        self._empty = 0  # frames in a row with no messages
        self._bottom = -math.inf  # the lowest row seen: where new messages come in
        # [message, slot, last frame seen, gone (True: from the top; HIDDEN: in empty frames),
        #  placed unaligned, first read at (``now``), its ``Doubt`` (None: not returned as new),
        #  times read again the same]
        self._seen: list[list] = []
        self.shown: set[str] = set()  # kinds of the messages in the last frame (new or not)
        self._misread: list[list] = []  # [message, slot, last frame, first at]: rows taken as a slot's misread
        self._last: list = []  # (message, y) of the last frame with messages

    def update(self, lines, now: float | None = None) -> list[Message]:
        self._frame += 1
        f, s = self._frame, self._scroll
        self._seen = [e for e in self._seen if f - e[2] <= self.keep_items]
        self._misread = [e for e in self._misread if f - e[2] <= self.keep_items]
        rows = [(m, r[1], r[2]) for r in visible_rows(lines) if (m := parse_message(r[0]))]
        self.shown = {m.kind for m, _, _ in rows}
        emptied, self._empty = self._empty >= 3, 0 if rows else self._empty + 1
        gap = emptied
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
        tried = []  # (messages lined up, which rows to which messages) per shift
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
            tried.append((len(used), frozenset((i, id(e)) for i, e in enumerate(match) if e is not None)))
        # Another shift lines up as many messages another way, and a row reads otherwise than
        # the last frame's row at its height (a misread): the scroll is a guess, so what is new is.
        changed = any(abs(y - py) <= tol and not _same(m, pm) for m, y, _ in rows for pm, py in self._last)
        guess = changed and bool(best[0]) and len({k for n, k in tried if n == best[0]}) > 1
        self._last = [(m, y) for m, y, _ in rows]
        # After empty frames, messages first read under ``young_s`` ago can't have faded (even if
        # a misread frame before marked them gone): this frame shows them, maybe misread, so what
        # is new in it may be one of them read again.
        young = gap and now is not None and any(
            e[5] is not None and now - e[5] < self.young_s for e in self._seen)
        for e in self._seen:
            if e[3] is HIDDEN:
                e[3] = True
        if emptied and best[0] and (best[0] >= self.rejoin or now is not None and all(
                now - e[5] < self.young_s for e in best[4] if e is not None and e[5] is not None)):
            emptied = False  # the hidden messages again, not new ones
        lined = best[0]  # hidden messages lined up, even too few to rejoin: maybe failed reads
        # The frame before showed messages, so a one-row frame where none of them lines up may be
        # one of them misread at a new place (a pair of new rows is a new kill and its bonus).
        after = len(rows) == 1 and not gap and any(f - e[2] <= 1 for e in self._seen)
        if best[0] and not emptied:
            self._scroll = s = s + best[3]
            match = best[4]
        else:  # nothing lines up (or the area was empty): what was shown faded
            best, match = (0,), [None] * len(rows)
        self._bottom = max(self._bottom, rows[-1][1])
        far = now - self.late_s if now is not None else math.inf  # first read before this: late
        out: list[Message] = []
        old = []  # per row: a message read before (None: new, skipped: neither)
        held = set()  # ids of messages whose slot shows a misread of them this frame
        for (m, y, _), e in zip(rows, match):
            relate = False  # new, but in a slot read last frame: maybe a misread of it
            if e is None:  # placed when nothing lined up, so maybe before a scroll: anywhere
                e = next((x for x in self._seen if x[4] and f - x[4] <= self.keep
                          and x[2] != f and _same(m, x[0])), None)
            if e is None and not emptied:  # a taken slot: a re-read, maybe misread
                # The same message first, then the one still up, then the latest seen.
                taken = sorted((x for x in self._seen if abs(x[1] - y - s) <= tol),
                               key=lambda x: (not _same(m, x[0]), bool(x[3]), -x[2]))
                e = taken[0] if taken else None
                if e is not None and not best[0] and y > self._bottom - tol and (
                        e[3] or f - e[2] > self.keep):
                    # A new message where a faded one was; late if a message read otherwise in
                    # its slot was seen just now (a dropped row can mark it gone): maybe it,
                    # misread, and that one maybe a misread of this (doubted).
                    recent = [x for x in taken if f - x[2] <= self.keep and not _same(m, x[0])]
                    for x in recent:
                        if x[6] is not None and x[7] < 2:
                            x[6].doubted = True
                    relate = bool(recent)
                    e = None
                elif e is not None and not (_same(m, e[0]) or m.item and e[0].kind == m.kind):
                    if e[6] is not None and e[7] < 2:
                        e[6].doubted = True  # read otherwise before twice the same: maybe a misread
                    if not best[0]:
                        relate = f - e[2] <= self.keep  # seen just now: maybe it, misread
                        e = None  # nothing lines up: a new message (late if it may be a misread)
                    else:  # a misread of the slot's message: noted, in case it is a new one
                        held.add(id(e))  # still up, read otherwise: not gone (nor seen)
                        x = next((x for x in self._misread if _same(m, x[0]) and abs(x[1] - y - s) <= tol), None)
                        if x is None:
                            self._misread.append([m, y + s, f, now])
                        else:
                            x[1], x[2] = y + s, f
                        old.append(False)
                        continue
            if e is not None:
                if e[4] and best[0]:  # placed unaligned: its frame-mates move with it
                    for x in self._seen:
                        if x[4] == e[4] and x is not e:
                            x[1], x[4] = x[1] + y + s - e[1], 0
                    e[4] = 0
                if e[2] != f and _same(m, e[0]):
                    e[7] += 1
                for x in self._seen:  # its slot read otherwise just before: that was maybe it, misread
                    if (x is not e and x[6] is not None and x[7] < 2 and f - x[2] <= self.keep
                            and abs(x[1] - y - s) <= tol and not _same(m, x[0])):
                        x[6].doubted = True
                e[1], e[2], e[3] = y + s, f, False
                old.append(e[5] if e[5] is not None else -math.inf)
                continue
            for x in self._seen:  # the slot's old message is gone
                if abs(x[1] - y - s) <= tol:
                    x[3] = True
            m = replace(m, doubt=Doubt())  # its own, shared with the copies returned
            self._seen.append([m, y + s, f, False, 0 if best[0] else f, now, m.doubt, 0])
            was = [x for x in self._misread if _same(m, x[0]) and abs(x[1] - y - s) <= tol]
            self._misread = [x for x in self._misread if x not in was]
            old.append(None)
            # In a slot still showing another message, or first taken as its misread: maybe
            # one message read two ways, so late (suspect), never a second trusted line.
            out.append(replace(m, late=True) if relate or was else m)
        news = [i for i, o in enumerate(old) if o is None]
        rejoined = gap and (lined or young)  # hidden messages showed again: reads failed, maybe not a fade
        rejoined = rejoined or after and not lined  # or a lone row lined up with nothing just after
        out = [replace(m, late=True) if rejoined or guess or any(o is not None and o is not False and o <= far
                                                        for o in old[i + 1:]) else m
               for i, m in zip(news, out)]
        top = rows[0][1] + s + tol
        for e in self._seen:  # gone with nothing shown above it: faded or scrolled off
            if e[2] != f and e[1] < top and id(e) not in held:
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

    In a party a kill's EXP is a share that varies with damage, and any price
    taken from a read value can be lowered by a misread (a lower price is
    extra kills). So party kills are counted from *events*, never magnitudes:

    - a **step** is one rise of the trusted bar, kept only if an EXP line was
      on screen within ``nearby_s`` of it. Rises within ``merge_s`` of a
      step's first rise, or while the bar is unsure, join that step (a kill's
      EXP and its bonus may land in different frames);
    - a **line** is one new kill line read. One with no bonus line that
      repeats a value read within ``repeat_s`` before it may be an old line
      read again: it is *suspect*, and so is one ``LineDiff`` placed late
      (``Message.late``: it may be older than ``match_s``); every other line
      is *trusted*;
    - ``M`` is a maximum matching of lines (trusted or suspect) to steps, one
      line per step, a line within ``match_s`` of the step's rises (from its
      first to its last: its kill may be at any of them);
    - ``count = trusted lines + steps - M``, booked as the matching settles.

    Invariant (why the count never passes the real kills): each trusted line
    is a distinct real kill, and its kill lies in at most one step, at a
    rise within ``match_s`` of the line. So the steps holding a trusted line's kill can
    be matched to distinct trusted lines: there are at most ``M`` of them.
    Suspect lines are not needed for that bound; matching them too keeps a
    kill read twice from counting twice when its EXP made two steps anyway.
    Every other step holds at least one kill whose line is not a trusted
    line, and distinct steps hold distinct kills. So ``count <= trusted
    lines + (steps holding no trusted line's kill) <= real kills``. Dropping
    an event (a step with no line near it, a step whose EXP the bar took
    back, events pending at a mode change) or merging two steps only lowers
    the count. Assumed: a kept step holds
    kill EXP, and a kill line shows within ``match_s`` of its step (measured
    on the owner's recording, see ``docs/decisions.md``). The cost is
    recall: kills sharing one bar step count once, a suspect line counts
    nothing. Line values only tell monsters apart (``KillCounter``).
    """

    nearby_s: float = 8.0  # an EXP line this close to a gain makes it kill EXP
    stale_s: float = 10.0  # solo: pool unconverted this long -> quarantined
    tol: int = 2  # EXP a sum may fall short by (rounding)
    max_parts: int = 20  # solo: a pool above this many of the largest value isn't decomposed
    recent_s: float = 30.0  # solo: a kill with no line must match a line value read this recently
    repeat_s: float = 8.0  # party: a line without its bonus repeating a value this recent may be a re-read
    flip_s: float = 2.0  # solo -> party: a pool this fresh is kept (its bonus line came just after)
    match_s: float = 6.0  # party: a kill line and its bar step are at most this far apart (L)
    merge_s: float = 0.5  # party: bar rises this close to a step's first rise are one step
    join_reads: int = 2  # party: rises a step takes for being seen in one reading (then it is closed)
    bonus_s: float = 0.5  # party: a bonus line belongs to a kill line read at most this long before
    fix_frac: float = 0.5  # party: a rise under this share of a step's EXP, with no second line, corrects it
    fix_s: float = 0.3  # party: ... counting lines from this long before the step's first rise
    fix_t: float = 3.5  # party: ... at most this long after it (a low bar read for 2-3 readings)
    total: int = 0
    quarantined: int = 0  # bar EXP not converted to kills
    counted: list = field(default_factory=list)  # (line value, party) of each line counted as a kill
    _high: int = 0
    _pool: int = 0
    _pool_t: float | None = None
    _line_t: float | None = None
    _lines: list = field(default_factory=list)  # uncounted lines: [time, value, EXP with bonus, bonus seen,
    #                                              value read within repeat_s before, placed late, Doubt]
    _party: bool = False
    _cache: tuple = ((), (0, 0))  # last decompose: (pool, values) -> result
    _seen: deque = field(default_factory=deque)  # (time, value) of recent lines
    clean: Counter = field(default_factory=Counter)  # solo: value -> times the gain was exactly one line's EXP
    _shown_t: float = -math.inf  # party: last frame showing an EXP line (new or not)
    _wait: list = field(default_factory=list)  # party: rises at the gate [first rise time, EXP,
    #                                             bar high after its last rise, bar readings seen
    #                                             at that high, rises joined for being seen once,
    #                                             the bar went unsure while it waited, last rise time]
    _bar_t: float | None = None  # party: when the last bar reading was taken (``bar_t``)
    _held: bool = False  # party: the bar was unsure at the last rise: the next rise joins its step
    _steps: list = field(default_factory=list)  # party: kept steps not yet decided (first, last rise times, EXP,
    #                                              EXP joined after it left the gate)
    _plines: list = field(default_factory=list)  # party: lines not yet decided (as in ``_lines``)

    def line(self, now: float, value: int = 0, late: bool = False, doubt: Doubt | None = None) -> None:
        """A new EXP line was read (``late``: ``LineDiff`` placed it late, see ``Message.late``;
        ``doubt``: its ``Message.doubt``, which ``LineDiff`` may set later)."""
        self._line_t = now
        again = any(v == value and 0 <= now - t <= self.repeat_s for t, v in self._seen)
        ln = [now, value, value, False, again, late, doubt]
        self._lines.append(ln)
        if self._party:
            self._plines.append(ln)
        self._seen.append((now, value))

    def shown(self, now: float) -> None:
        """An EXP line is on screen, new or not: kill gain near it is a kill's
        (a message area full of the same line shows nothing new for a while)."""
        self._shown_t = now

    def bonus(self, value: int, now: float | None = None) -> None:
        """A party bonus line: it belongs to the line just before it (read
        within ``bonus_s`` of it, when ``now`` is given)."""
        if self._lines and not self._lines[-1][3] and (now is None or now - self._lines[-1][0] <= self.bonus_s):
            self._lines[-1][2] += value
            self._lines[-1][3] = True

    def pair(self, total: int, bonus: int = 0) -> None:
        """A kill + bonus pair read together. Party kills price nothing, so
        it is ignored (kept for the probes and tests that replay reads)."""

    def boundary(self) -> None:
        """A new party or share. Party kills use no magnitudes, so nothing
        starts over (the session still calls it on a party size change)."""

    def update(self, gain: int, now: float, solo_values: Sequence[int] = (), party: bool = False,
               hold: bool = False, wrap: bool = False, unconfirmed: int = 0,
               bar_t: float | None = None) -> int:
        """Book new bar gain; -> kills added. ``hold``: the bar is unsure,
        wait. ``wrap``: this gain spans a level-up (unused: kills count the
        same either side of one). Party only: ``unconfirmed`` is the part of
        ``gain`` the bar may still take back (``ExpTracker.unconfirmed_gain``),
        ``bar_t`` when the last bar reading was taken (``last_committed_t``):
        a step waits until its gain is confirmed (see ``_party_update``)."""
        if party != self._party:  # the pool was gathered under the other rule
            self._party = party
            if party:
                self._plines = list(self._lines)  # lines the solo rule hasn't counted
                if self._pool_t is not None and now - self._pool_t <= self.flip_s:
                    # A first kill after a pause reads as solo until its bonus line
                    # shows: a pool that fresh is the party kill's, not a solo one's.
                    self._wait.append([self._pool_t, self._pool, self._high, 2, 0, False, now, 0])
                    self._pool = 0
            else:  # undecided party events are dropped: that only lowers the count
                self.quarantined += sum(w[1] for w in self._wait)
                self._wait, self._steps, self._plines, self._lines = [], [], [], []
                self._held = False
            self._quarantine()
        if party and gain < self._high:
            self._take_back(self._high - gain)
        new = max(0, gain - self._high)
        self._high = max(self._high, gain)
        self._lines = [ln for ln in self._lines if now - ln[0] <= self.nearby_s]
        while self._seen and now - self._seen[0][0] > self.recent_s:
            self._seen.popleft()
        if party:
            return self._party_update(new, now, hold, gain - unconfirmed, bar_t)
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

    def _take_back(self, fall: int) -> None:
        """Party: the bar fell (a misread rise taken back): take it out of the
        rises still at the gate, newest first, then the EXP joined to the newest
        kept step after it left the gate (``_fixes``), then that step if not yet
        decided. Only ever removes steps."""
        while fall > 0 and self._wait:
            w = self._wait[-1]
            take = min(fall, w[1])
            w[1] -= take
            w[2] -= take
            w[3] = 0  # a new high: seen in no reading yet (this one counts next)
            w[5] = True  # the bar misread near it: it waits for two readings at its high
            self._high -= take
            fall -= take
            if w[1] <= 0:
                self._wait.pop()
        if fall > 0 and self._steps and self._steps[-1][3]:
            first, last, exp, fixed = self._steps[-1]  # a rise joined to it after the gate (a spike?)
            take = min(fall, fixed)
            self._steps[-1] = (first, last, exp - take, fixed - take)
            self._high -= take  # as if it had waited at the gate on its own
            fall -= take
        if fall > 0 and self._steps:
            # More than the gate holds: a step that left it (its high read twice, a spike read
            # twice) held EXP the bar took back. Drop the newest one not yet decided.
            self._steps.pop()

    def _party_update(self, new: int, now: float, hold: bool, settled: int,
                      bar_t: float | None) -> int:
        """Rises become steps. A rise joins the step before it if that step
        is within ``merge_s``, rose while the bar was unsure, or was seen in
        one bar reading only (a reading a little low, then the true one), or
        the bar went unsure since with under two readings after (two misreads
        in a row can leave a few EXP booked until the next kill); those two
        at most ``join_reads`` times, so a rise at every reading (kills
        faster than the bar is read) still makes steps that settle. A
        step leaves the gate only once the bar has confirmed all of its gain
        (``settled``) or shown its high in two readings: a one-reading spike
        is taken back first (see ``_take_back``). On a still bar the tracker
        confirms a step only at the next kill, so the second rule keeps the
        last kill from waiting that long. Merging and waiting only ever lower
        the count."""
        reading = bar_t is None or bar_t != self._bar_t
        self._bar_t = bar_t
        w = self._wait[-1] if self._wait else None
        if new and not w and self._steps:
            first, last, exp, fixed = self._steps[-1]
            if self._fixes(new, now, first, exp, fixed):  # left the gate but undecided: the same kill
                self._steps[-1] = (first, now, exp + new, new)
                new = 0
        if new:
            near = self._held or now - w[0] <= self.merge_s if w else False
            if w is not None and not near and self._fixes(new, now, w[0], w[1], w[7]):
                near, w[7] = True, new
            if w and (near or w[3] <= 1 and w[4] < self.join_reads):
                w[1] += new  # the same step: one kill's EXP and bonus in two frames
                w[2] = self._high
                w[4] += not near  # joined for being seen once: at most join_reads times
                w[3] = 1 if reading else w[3]  # readings seen at its new high
                w[6] = now  # its last rise: its kill may be at any of its rises
            else:
                # Readings unknown (no ``bar_t``): never joined for being seen once.
                self._wait.append([now, new, self._high, 1 if bar_t is not None else 2, 0, False, now, 0])
        elif w and reading:
            w[3] += 1
        if hold and self._wait:  # unsure now: until two sure readings, the next rise joins it
            self._wait[-1][3] = 0  # and it waits for them (the tracker may be the one misled)
            self._wait[-1][5] = True
        self._held = hold and (bool(new) or self._held)
        while self._wait and not hold:  # in time order, so steps reach the matching in order
            w = self._wait[0]
            if now - w[0] <= self.merge_s or (settled < w[2] or w[5]) and w[3] < 2:
                break  # it may still grow, or the bar may still take its gain back
            if max(self._line_t or -math.inf, self._shown_t) >= w[0] - self.nearby_s:
                self._steps.append((w[0], w[6], w[1], w[7]))  # an EXP line was on screen near it: a kill step
            elif now - w[0] > self.nearby_s:
                self.quarantined += w[1]  # no kill line anywhere near it
            else:
                break  # its line may still show
            self._wait.pop(0)
        return self._match(now)

    def _fixes(self, new: int, now: float, first: float, exp: int, fixed: int) -> bool:
        """A rise that corrects the step that began at ``first`` (holding ``exp``,
        ``fixed`` of it from a correction): its first correction, within
        ``fix_t`` of it, under ``fix_frac`` of it, with at most one kill line
        read from ``fix_s`` before the step until now (its own, maybe read
        late: a second line is a second kill). A bar read a few EXP low for
        two or more readings, then true, would otherwise make a second step
        for one kill. Only merges steps (two kills merged lose one, never add
        one); once per step, so kills whose lines aren't new (an area full of
        one line) don't all join one step."""
        lines = sum(1 for t, _ in self._seen if t >= first - self.fix_s)
        return (not fixed and now - first <= self.fix_t and lines <= 1
                and new < self.fix_frac * exp)

    @staticmethod
    def _trusted(ln: list) -> bool:
        """Not a line read again or placed late: it has its bonus line, or no
        line read just before it had its value; and ``LineDiff`` didn't place
        it late (it may be seconds older than its read, beyond ``match_s``)."""
        return not ln[5] and (ln[3] or not ln[4]) and not (ln[6] and ln[6].doubted)

    def _match(self, now: float) -> int:
        """Greedy matching of lines to steps in time order, deciding only what
        no event still to come can change; -> kills booked. Every step to come
        is at least as late as the first still at the gate (or ``now``), every
        line to come is at least as late as ``now``. A line may match a step
        from ``match_s`` before its first rise to ``match_s`` after its last
        (its kill may be at any of its rises). Steps are disjoint and in time
        order (only the newest one at the gate grows), so these windows are in
        order at both ends and the greedy matching is maximum."""
        L = self.match_s
        nxt = min((w[0] for w in self._wait), default=now)
        add = 0
        while self._plines or self._steps:
            ln = self._plines[0] if self._plines else None
            s = self._steps[0] if self._steps else None
            if ln is not None and s is not None and s[1] < ln[0] - L:
                self._steps.pop(0)  # no line near it, now or to come: a kill whose line wasn't read
                add += 1
                continue
            if ln is None:
                if now - s[1] <= L:
                    break  # its line may still come
                self._steps.pop(0)
                add += 1
                continue
            if now - ln[0] < L:
                break  # its bonus line may still come (it decides trusted or not)
            if s is not None and s[0] - L <= ln[0] <= s[1] + L:
                self._steps.pop(0)  # matched: the step holds this line's kill (if any)
            elif s is None and nxt - ln[0] <= L:
                break  # its step may still come
            self._plines.pop(0)  # matched, or no step near it now or to come
            if self._trusted(ln):
                self.counted.append((ln[1], True))
                add += 1
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

