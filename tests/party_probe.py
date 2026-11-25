"""Full-pipeline party probes from the PR #31 reviews (message area -> LineDiff -> BarKills,
bar -> ExpTracker), as the review scripts ran them (r6_probe.py, x_probe.py; their random streams
are kept so seeds match). Each returns (real kills, party total, the most the total ever passed real kills).

run_r6 gens: base, mis1, gapread (3-6 failed reads while lines stay up), barlow (bar read low once),
barspike (read high once), susp, suspfast (one mob, bonus lines missed, bar errors and read failures).
run_x gens: barlow2 (bar read low for 2-3 readings), loot (mesos and item lines among kill lines),
and the others named in its body. ``mis`` scales the per-row digit misread rate (0: off).

``colours=True`` (#30) also draws each kill's line colour (white: your own kill, yellow: a party
share; bonus lines yellow, loot lines white) from a separate random stream, so the seeds' kills
and reads don't change, and reads a row's colour as unknown at a rate scaled by ``mis`` (a colour
misread: the classifier abstains). It then returns a dict: real / total / peak as before, and for
"self" (white lines) and "party" (yellow lines) the real count, the count and the most it passed
real."""
import random
from dataclasses import dataclass

from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.exp_tracker import ExpTracker, Reading
from mctracker.kills import BarKills, LineDiff

K = lambda v: f"You have received EXP (+{v})"  # noqa: E731
B = lambda v: f"Bonus EXP for PARTY (+{v})"  # noqa: E731
LV = 50
NEED = EXP_TABLE[LV]

@dataclass
class Row:
    """A read row as the message area gives ``LineDiff`` (no height or x, as the tuples)."""
    text: str
    y: float
    colour: str | None = None
    height: float = 0.0
    x0: float = 0.0


class Colours:
    """One run's colour truth and colour reads, from its own random stream."""

    def __init__(self, tag: str, mis: float):
        self.rnd = random.Random(f"colour-{tag}")
        self.p_own = self.rnd.choice([0.0, 0.3, 0.5, 0.8])  # share of kills that are yours
        self.p_unk = self.rnd.choice([0.02, 0.1, 0.3]) * mis  # a row's colour read as unknown
        self.real = {"self": 0, "party": 0}
        self.peak = {"self": 0, "party": 0}

    def kill(self) -> str:
        c = "white" if self.rnd.random() < self.p_own else "yellow"
        self.real["self" if c == "white" else "party"] += 1
        return c

    def read(self, txt: str, colour: str | None, y: float) -> Row:
        return Row(txt, y, None if self.rnd.random() < self.p_unk else colour)

    def check(self, k: BarKills) -> None:
        for kind, key in (("self", "white"), ("party", "yellow")):
            self.peak[kind] = max(self.peak[kind], k.by[key] - self.real[kind])

    def result(self, real, k: BarKills, peak) -> dict:
        out = {"real": real, "total": k.total, "peak": peak, "unknown": k.by["unknown"] + k.by["step"]}
        for kind, key in (("self", "white"), ("party", "yellow")):
            out.update({f"real_{kind}": self.real[kind], kind: k.by[key], f"peak_{kind}": self.peak[kind]})
        return out


def drop_digit(v, rnd):
    s = str(v)
    if len(s) < 2:
        return v
    i = rnd.randrange(len(s))
    return int(s[:i] + s[i + 1:]) or v



def run_r6(seed, gen, mis=1.0, colours=False):
    rnd = random.Random(f"r4-{gen}-{seed}")
    col = Colours(f"r6-{gen}-{seed}", mis) if colours else None
    mobs = [rnd.choice([300, 800, 1000, 2500]) for _ in range(rnd.randint(1, 3))]
    ratio = rnd.choice([0.05, 0.1, 0.2])
    fade = rnd.uniform(7, 11); rows_max = rnd.choice([2, 4, 6])
    gap = rnd.choice([(0.5, 2), (2, 6), (6, 14)])          # sparse areas: 1-2 lines up
    p_mis = rnd.choice([0.02, 0.05, 0.1]) * mis if gen != "base" or mis else 0.0
    if gen == "base":
        p_mis = rnd.choice([0, 0.02]) * mis
    p_row = rnd.choice([0, 0.05])                           # a row not read this frame
    p_fail = 0.03 if gen == "gapread" else 0.0              # start of a failed-read burst
    p_barlow = 0.3 if gen == "barlow" else 0.0
    p_spike = 0.05 if gen == "barspike" else 0.0
    p_nobonus = 0.0
    if gen == "susp":  # suspect lines (one mob, bonus lines missed) with every step and read failure
        mobs = mobs[:1]; p_nobonus = 0.7; p_barlow = 0.15; p_spike = 0.03; p_fail = 0.015
    if gen == "suspfast":  # the same, kills faster than the bar is read
        mobs = mobs[:1]; p_nobonus = 0.7; p_barlow = 0.15; p_spike = 0.03; p_fail = 0.015
        gap = (0.4, 1.5)
    bar_dt = rnd.choice([0.2, 1.0])
    d, k, e = LineDiff(), BarKills(), ExpTracker()
    area = []                                               # (text, born)
    t, x, real, peak, fail_until = 0.0, 200000, 0, 0, -1
    next_kill = rnd.uniform(*gap); next_bar = 0.0; lowpend = None
    k.update(0, 0.0, party=True)
    while t < 300:
        t = round(t + 0.2, 3)
        if t >= next_kill and t < 280:
            next_kill = t + rnd.uniform(*gap)
            s = rnd.choice(mobs); b = max(1, round(s * ratio))
            real += 1; x += s + b
            c = col.kill() if col else None
            area += [(K(s), t, c)] + ([] if rnd.random() < p_nobonus else [(B(b), t, "yellow")])
            if rnd.random() < p_barlow:
                lowpend = x - rnd.randint(5, 60)
        area = [a for a in area if t - a[1] < fade][-rows_max:]
        if p_fail and t > fail_until and rnd.random() < p_fail:
            fail_until = t + 0.2 * rnd.randint(3, 6)
        frame = []
        if t > fail_until:
            n = len(area)
            for i, (txt, born, c) in enumerate(area):
                if rnd.random() < p_row:
                    continue
                if rnd.random() < p_mis:
                    v = int(txt.split("+")[1][:-1]); txt = txt.replace(f"+{v})", f"+{drop_digit(v, rnd)})")
                y = 100.0 - 12.0 * (n - 1 - i)
                frame.append(col.read(txt, c, y) if col else (txt, y))
        for m in d.update(frame, now=t):
            if m.kind == "exp":
                k.line(t, m.value, m.late, m.doubt, m.colour)  # as Session passes
            elif m.kind == "party_bonus":
                k.bonus(m.value, t)
        if "exp" in d.shown:
            k.shown(t)
        if t >= next_bar - 1e-9:
            next_bar = t + bar_dt
            v = x
            if lowpend is not None:
                v, lowpend = lowpend, None
            elif rnd.random() < p_spike:
                v = x + rnd.randint(5, 60)
            e.add(Reading(t, LV, v, bar_percent(v, NEED)), now=t)
        g = e.current_gain  # as Session passes in a party
        k.update(g, t, party=True, hold=e.behind, unconfirmed=e.unconfirmed_gain, bar_t=e.last_committed_t)
        peak = max(peak, k.total - real)
        col and col.check(k)
    for _ in range(60):
        t += 0.2; k.update(g, t, party=True, unconfirmed=e.unconfirmed_gain, bar_t=e.last_committed_t); peak = max(peak, k.total - real)
        col and col.check(k)
    return col.result(real, k, peak) if col else (real, k.total, peak)


def run_x(seed, gen, mis=1.0, trace=False, colours=False):
    rnd = random.Random(f"x-{gen}-{seed}")
    col = Colours(f"x-{gen}-{seed}", mis) if colours else None
    LV = 50
    mobs = [rnd.choice([300, 800, 1000, 2500]) for _ in range(rnd.randint(1, 3))]
    ratio = rnd.choice([0.05, 0.1, 0.2])
    fade = rnd.uniform(7, 11); rows_max = rnd.choice([2, 4, 6])
    gap = rnd.choice([(0.5, 2), (2, 6), (6, 14)])
    p_mis = rnd.choice([0.0, 0.02, 0.05]) * mis
    p_row = rnd.choice([0, 0.05])
    p_nobonus = 0.0
    low_hold = 0; spike_hold = 0; p_low = 0.0; p_spike = 0.0
    p_loot = 0.0; p_newlag = 0.0; pick = []
    p_bonuslate = 0.0; p_burst = 0.0; p_flip = 0.0; p_rowfail = 0.0; p_dup = 0.0
    x = 200000
    if gen == "barlow2":
        p_low = 0.2
    elif gen == "spike2":
        p_spike = 0.05
    elif gen == "samev":
        mobs = mobs[:1]; ratio = 0.1; fade = rnd.uniform(6, 9); gap = (fade - 1.0, fade + 1.0)
        rows_max = rnd.choice([2, 4])
    elif gen == "bonuslate":
        p_bonuslate = 0.3; p_nobonus = 0.2
    elif gen == "burst":
        p_burst = 0.2
    elif gen == "scrolloff":
        rows_max = 2; gap = (0.4, 1.5)
    elif gen == "flip":
        p_flip = 0.01
    elif gen == "levelup":
        x = EXP_TABLE[LV] - rnd.randint(1000, 20000)
    elif gen == "rowfail":
        p_rowfail = 0.03
    elif gen == "dup":
        p_dup = 0.02
    elif gen in ("loot", "lootfast"):
        p_loot = 0.8; p_newlag = 0.3
        if gen == "lootfast":
            gap = (0.4, 2.0)
    bar_dt = rnd.choice([0.2, 1.0])
    d, k, e = LineDiff(), BarKills(), ExpTracker()
    area = []  # [text, born, skip_until]
    t, real, peak = 0.0, 0, 0
    next_kill = rnd.uniform(*gap); next_bar = 0.0
    held = None; held_n = 0; party_until = -1; party = True
    k.update(0, 0.0, party=True)
    while t < 300:
        t = round(t + 0.2, 3)
        if t >= next_kill and t < 280:
            next_kill = t + rnd.uniform(*gap)
            n = 1 + (rnd.random() < p_burst) * rnd.randint(1, 2)
            for _ in range(n):
                s = rnd.choice(mobs)
                if gen != "samev":
                    s = max(1, round(s * rnd.choice([1.0, 1.0, 0.8, 0.6])))  # damage share
                b = max(1, round(s * ratio))
                real += 1; x += s + b
                area.append([K(s), t, t + 0.2 if rnd.random() < p_newlag else -1, col.kill() if col else None])
                if rnd.random() >= p_nobonus:
                    area.append([B(b), t, t + 0.2 * rnd.randint(1, 4) if rnd.random() < p_bonuslate else -1,
                                 "yellow"])
            for _ in range(rnd.randint(1, 2) if rnd.random() < p_loot else 0):
                pick.append((t + rnd.uniform(0.3, 3.0), rnd.choice(["You have gained mesos (+%d)" % rnd.choice([12, 15, 30]),
                                                                   "Snail Shell x1 earned. (Etc)", "Red Potion x1 earned. (Use)"])))
            if rnd.random() < p_low:
                held = x - rnd.randint(5, 60); held_n = rnd.randint(2, 3)
            elif rnd.random() < p_spike:
                held = x + rnd.randint(5, 60); held_n = 2
        for pt, txt in [q for q in pick if q[0] <= t]:
            area.append([txt, t, t + 0.2 if rnd.random() < p_newlag else -1, "white"])
        pick = [q for q in pick if q[0] > t]
        if gen == "levelup" and x >= EXP_TABLE[LV]:
            x -= EXP_TABLE[LV]; LV += 1
        area = [a for a in area if t - a[1] < fade][-rows_max:]
        if p_rowfail and area and rnd.random() < p_rowfail:
            a = rnd.choice(area); a[2] = t + 0.2 * rnd.randint(2, 8)
        frame = []
        nrow = len(area)
        off = 12.0 if p_dup and rnd.random() < p_dup else 0.0
        for i, (txt, born, skip, c) in enumerate(area):
            if t < skip or rnd.random() < p_row:
                continue
            if rnd.random() < p_mis and "EXP" in txt:
                v = int(txt.split("+")[1][:-1]); txt = txt.replace(f"+{v})", f"+{drop_digit(v, rnd)})")
            y = 100.0 - 12.0 * (nrow - 1 - i) - off
            frame.append(col.read(txt, c, y) if col else (txt, y))
        if p_flip:
            if party and rnd.random() < p_flip:
                party = False; party_until = t + rnd.uniform(1, 3)
            elif not party and t >= party_until:
                party = True
        for m in d.update(frame, now=t):
            if m.kind == "exp":
                k.line(t, m.value, m.late, m.doubt, m.colour)
                if trace:
                    print(f"{t:7.1f} line {m.value} late={m.late}")
            elif m.kind == "party_bonus":
                k.bonus(m.value, t)
        if "exp" in d.shown:
            k.shown(t)
        if t >= next_bar - 1e-9:
            next_bar = t + bar_dt
            v = x
            if held is not None:
                v = held; held_n -= 1
                if held_n <= 0:
                    held = None
            e.add(Reading(t, LV, v, bar_percent(v, EXP_TABLE[LV])), now=t)
        g = e.current_gain
        k.update(g, t, party=party, hold=e.behind, unconfirmed=e.unconfirmed_gain, bar_t=e.last_committed_t)
        if trace and k.total - real > peak:
            print(f"{t:7.1f} OVER total {k.total} real {real}")
        peak = max(peak, k.total - real)
        col and col.check(k)
    for _ in range(60):
        t += 0.2; k.update(g, t, party=party, unconfirmed=e.unconfirmed_gain, bar_t=e.last_committed_t)
        peak = max(peak, k.total - real)
        col and col.check(k)
    return col.result(real, k, peak) if col else (real, k.total, peak)
