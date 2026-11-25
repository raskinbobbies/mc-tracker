"""Party kills undercount only (#27): a mob's counted kills never exceed its true kills,
even under an EXP event, with fixed share ratios, strays and untrusted solo fits."""

import random
from collections import Counter

from mctracker.exp_table import EXP_TABLE, bar_percent
from mctracker.kills import KillCounter, Mob, parse_message
from mctracker.session import Tracker

A, B = Mob(1, "A", 300), Mob(2, "B", 700)


def test_event_shares_with_no_solo_lines_do_not_reach_the_top_mob():
    # 1.5x event: A's shares at f=0.9 are 405, B's at f=0.5 are 525. Both sit above
    # A's 1x value, but A could give 600 at 2x, so neither is credited to B.
    kc = KillCounter()
    kc.set_map([A, B])
    for _ in range(10):
        kc.add(405, party=True)
    for _ in range(5):
        kc.add(525, party=True)
    c = kc.counts()
    assert c[1] <= 10 and c[2] <= 5, c  # was {B: 15}


def test_one_solo_cluster_does_not_place_the_window():
    # 2x event: 3 solo kills of the 54 mob (108 each) fit a 1x scale onto the 108..130
    # range; b's shares (88) must not then count for c, which was never killed.
    a, b, c = Mob(1, "a", 54), Mob(2, "b", 63), Mob(3, "c", 130)
    kc = KillCounter()
    kc.set_map([a, b, c])
    for _ in range(3):
        kc.add(108)
    for _ in range(10):
        kc.add(88, party=True)
    n = kc.counts()
    assert n[3] == 0 and n[2] <= 10 and n[1] <= 3, n  # was {c: 13}


# --- session-level probe ---------------------------------------------------------

MAPS = [[300, 700], [400, 1000, 3000], [100, 210, 450], [54, 63, 130], [600, 700],
        [54, 63, 130, 135], [50, 120, 260, 270]]  # the last two: mobs within cluster_tol
BASE = 100_000


def _status(tr, exp, now):
    tr.on_status(45, exp, bar_percent(exp, EXP_TABLE[45]), now=now)


def _session_kills(tmp_path, mobs, first, lines, party_size=2):
    """Kills of a party session fed ``lines``: (shown value, EXP gained, has a bonus
    line). ``first`` starts the session as a party share."""
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("X", mobs)
    tr.party_size = party_size
    bar = BASE
    _status(tr, bar, 0)
    _status(tr, bar, 0.5)
    tr.on_message_lines([(f"You received EXP (+{first})", 800)], now=1)
    s = tr.session
    s._bonus_t = None
    s.killer.solo = False
    s.killer.party[0] = True
    now, bar = 2.0, bar + first
    for v, gain, bonus in lines:
        msgs = [parse_message(f"You received EXP (+{v})")]
        if bonus:
            msgs.append(parse_message(f"Bonus EXP for PARTY (+{bonus})"))
        else:
            s._bonus_t = None  # no bonus line: flagged solo
        for msg in msgs:
            s.on_message(msg, now=now)
        s.bar.shown(now)
        s.on_lines(msgs, now=now)
        bar += gain + bonus
        _status(tr, bar, now + 0.3)
        _status(tr, bar, now + 0.6)
        s.on_lines([], now=now + 1)
        now += 3
    _status(tr, bar, now + 20)
    s.on_lines([parse_message("You received EXP (+1)")], now=now + 25)
    return s.kills


def test_missed_bonus_lines_do_not_fit_a_solo_scale():
    # 1x: A's shares are 240, B's 350; 3 more of A's shares (150) lost their bonus line
    # and look solo. One cluster fits a 0.5x scale that would pull the window down
    # over A's 240s.
    kc = KillCounter()
    kc.set_map([A, B])
    for _ in range(3):
        kc.add(150)
    for _ in range(10):
        kc.add(240, party=True)
        kc.add(350, party=True)
    n = kc.counts()
    assert n[1] <= 13 and n[2] <= 10, n  # was {B: 20, A: 3}


def _party_run(seed, tmp_path):
    """One random party session; returns (mobs, kills, truth)."""
    r = random.Random(seed)
    mobs = [Mob(i + 1, f"M{i}", e) for i, e in enumerate(r.choice(MAPS))]
    ev = r.choice([1.0, 1.5, 2.0])
    fixed_ratio = r.random() < 0.3  # each mob's share is the same every kill
    ratio = {m.id: r.uniform(0.3, 1.0) for m in mobs}
    n = r.randint(20, 50)
    strays = {i: r.choice(["misread", "dropdigit", "solo"]) for i in r.sample(range(n), r.randint(0, 3))}
    # Fixed-ratio runs may also lose bonus lines often (each such share is flagged solo
    # and repeats, so it passes the seen-twice check); any line is misread 3% of the time.
    p_missed = r.choice([0.0, 0.15]) if fixed_ratio else 0.0
    party_size = r.randint(2, 6)
    m0 = r.choice(mobs)
    first = max(1, int(m0.exp * ev * r.uniform(0.3, 1.0)))
    truth, lines = Counter({m0.id: 1}), []
    for i in range(n):
        m = r.choice(mobs)
        truth[m.id] += 1
        share = max(1, int(m.exp * ev * (ratio[m.id] if fixed_ratio else r.uniform(0.3, 1.0))))
        kind = strays.get(i) or ("misread" if r.random() < p_missed else None)
        if kind == "solo":  # a real solo kill at full value
            v = int(m.exp * ev)
            lines.append((v, v, 0))
        elif kind == "misread":  # a share whose bonus line was missed: flagged solo
            lines.append((share, share, 0))
        elif kind == "dropdigit":  # the share read a digit short
            lines.append((max(1, share // 10), share, max(1, share // 10)))
        else:
            v = share
            if r.random() < 0.03:  # misread: an extra digit, a leading 1, or a lost digit
                v = r.choice([v * 10 + r.randint(0, 9), int("1" + str(v)), v // 10 or 1])
            lines.append((v, share, max(1, share // 10)))
    return mobs, _session_kills(tmp_path / str(seed), mobs, first, lines, party_size), truth


def test_party_sessions_never_overcount_a_mob(tmp_path):
    over = []
    for seed in range(300):
        mobs, kills, truth = _party_run(seed, tmp_path)
        bad = {m.exp: (kills[m.id], truth[m.id]) for m in mobs if kills[m.id] > truth[m.id]}
        if bad:
            over.append((seed, bad))
    assert not over, over[:5]
