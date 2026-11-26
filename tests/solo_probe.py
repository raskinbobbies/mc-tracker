"""Full-pipeline solo probe (message area -> LineDiff -> BarKills, bar -> ExpTracker -> Session),
like party_probe.py. A solo grind on one map; returns (real kills, counted kills, the most the
count ever passed real kills).

The message area keeps the newest ``rows`` lines and fades each after ``fade`` s (an assumed model:
6 rows and a 9 s fade). Every kill line of one monster is the same text, so an area full of them
shows ``LineDiff`` nothing new: the bar alone counts those kills.

Options: ``mobs`` (EXP per monster, one picked per kill), ``gap`` (s between hits), ``aoe`` (kills
per hit), ``lag`` (s a kill's line shows after its EXP lands: the bar is read first), ``spikes``
(times of one bar reading misread high by ``spike`` EXP, then read right), ``deaths`` (times of a
death: the bar drops a tenth of the level; no kill lands in the 2 s before it), ``map_mobs`` (the
map's monster list, if not ``mobs``: one missing from it is a monster the data doesn't list)."""
import random
import tempfile
from pathlib import Path

from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.kills import Mob
from mctracker.session import Tracker

K = lambda v: f"You have received EXP (+{v})"  # noqa: E731
LV = 50


def run(seed, mobs=(390,), gap=(1.0, 2.0), aoe=(1, 1), lag=0.0, spikes=(), spike=7800, deaths=(),
        bar_dt=1.0, line_dt=0.15, rows=6, fade=9.0, T=240.0, map_mobs=None, bar_draw=False):
    # bar_draw: draw one random number per bar reading (the advisors' sim64 stream, its p_mis=0)
    rnd = random.Random(seed)
    tr = Tracker(save_dir=Path(tempfile.mkdtemp()))
    tr.on_map("Probe", [Mob(i + 1, f"m{i}", v) for i, v in enumerate(mobs if map_mobs is None else map_mobs)])
    need = EXP_TABLE[LV]
    x, real, peak, area, t = 200_000, 0, 0, [], 0.0
    nk, nb, nl = 1.0, 0.0, 0.0
    spikes, deaths = list(spikes), list(deaths)
    while t < T:
        t = round(t + 0.05, 3)
        if deaths and t >= deaths[0]:
            deaths.pop(0)
            x -= need // 10
            nk = t + 3
        near_death = any(0 <= d - t <= 2 for d in deaths)
        if t >= nk and t < T - 20 and not near_death:
            nk = t + rnd.uniform(*gap)
            for _ in range(rnd.randint(*aoe)):
                v = rnd.choice(mobs)
                real += 1
                x += v
                area.append((K(v), t + lag))
        area = [a for a in area if t - a[1] < fade][-rows:]
        if t >= nl:
            nl = t + line_dt
            vis = [a for a in area if a[1] <= t]
            tr.on_message_lines([(txt, 100.0 - 12.0 * (len(vis) - 1 - i)) for i, (txt, _) in enumerate(vis)], now=t)
        if t >= nb:
            nb = t + bar_dt
            shown = x
            if bar_draw:
                rnd.random()
            if spikes and t >= spikes[0]:
                spikes.pop(0)
                shown = x + spike
            tr.on_status(LV, shown, bar_percent(shown, need), now=t)
        if tr.session:
            tr.session.tick(t)
            peak = max(peak, tr.session.total_kills - real)
    s = tr.session
    s.finalize(t)
    return real, s.total_kills, max(peak, s.total_kills - real)


CASES = {
    "identical area, kill every 0.5-1 s": dict(gap=(0.5, 1.0)),
    "identical area, kill every 1-2 s": dict(),
    "multi-kill hits, two mobs": dict(mobs=(390, 550), gap=(3, 5), aoe=(2, 3)),
    "bar read before the line": dict(mobs=(390, 550), lag=1.5),
    "a misread bar spike": dict(mobs=(390, 550), gap=(2, 4), spikes=(60.3,)),
    "a death": dict(mobs=(390, 550), gap=(2, 4), deaths=(100,)),
    # 940 = 390 + 550: the fewest sum of listed values passes the kills until 940 is trusted.
    "a monster the map list lacks": dict(mobs=(390, 550, 940), map_mobs=(390, 550)),
    "a monster the map list lacks, 10 min": dict(mobs=(390, 550, 940), map_mobs=(390, 550), T=600.0),
}
# Seeds of a case once miscounted (tested besides seeds 0-2). sim64 seed 7 (+1): a pool of the first
# lines read, 940 among them untrusted, booked as 390s and 550s; a 390 then went ahead of a line
# already read. Seeds 4 and 5 were under.
OFF_SEEDS = {"sim64 stream, 940 missing from the list": (dict(mobs=(390, 550, 940), map_mobs=(390, 550),
                                                                 bar_draw=True), (4, 5, 7))}

if __name__ == "__main__":
    for name, kw in CASES.items():
        rs = [run(i, **kw) for i in range(8)]
        print(f"{name:38s} real {sum(r[0] for r in rs)} counted {sum(r[1] for r in rs)} "
              f"max over {max(r[2] for r in rs)}")
