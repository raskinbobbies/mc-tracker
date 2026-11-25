"""Party BarKills overcount paths found in review of #31, plus a seeded random
probe: whatever the reads, the count must never pass the real kills."""
import random

import pytest

from mctracker.kills import BarKills


class Sim:
    """Kills 2 s apart; the bar is read at the kill and 1 s after it."""

    def __init__(self):
        self.k, self.t, self.gain, self.real = BarKills(), 0.0, 0, 0

    def kill(self, exp, bonus, read_kill=None, read_bonus=None):
        self.t += 2
        self.real += 1
        self.gain += exp + bonus
        if read_kill is not None:
            self.k.line(self.t, read_kill)
            if read_bonus is not None:
                self.k.bonus(read_bonus)
                self.k.pair(read_kill + read_bonus, read_bonus)
        for dt in (0, 1):
            self.k.shown(self.t + dt)
            self.k.update(self.gain, self.t + dt, party=True)

    def idle(self, secs=30):
        for _ in range(int(secs * 2)):
            self.t += 0.5
            self.k.shown(self.t)
            self.k.update(self.gain, self.t, party=True)


def test_bonus_lines_missed_before_any_bonus_is_seen_are_not_counted_twice():
    # No bonus ratio yet: a kill line alone can't be priced, so the bonus EXP it
    # leaves in the gain must not come back as kills through // U.
    s = Sim()
    for _ in range(6):
        s.kill(45, 9, read_kill=45)
    for _ in range(3):
        s.kill(45, 9, 45, 9)
    s.idle()
    assert s.k.total <= s.real == 9


def test_a_big_mob_line_read_with_a_digit_dropped_costs_its_pair():
    # 300+30 and 2000+200 on one map; "2000" read as "200" with its bonus line
    # dropped is well above half the smallest pair, but it is the big mob's.
    s = Sim()
    for _ in range(3):
        s.kill(300, 30, 300, 30)
        s.kill(2000, 200, 2000, 200)
    s.idle()
    for _ in range(3):
        s.kill(2000, 200, read_kill=200)
        s.idle(10)
    s.idle()
    assert s.k.total <= s.real == 9


def test_screen_pairs_include_a_mob_whose_bonus_was_never_read():
    # A 20+2 known; C 21+2 on screen but its bonus never read. After a new
    # epoch, screen pairs from A steps must not leave U at A's 22.
    s = Sim()
    for _ in range(3):
        s.kill(20, 2, 20, 2)
    s.idle()
    s.kill(21, 2, read_kill=21)
    s.idle(15)
    s.k.boundary()
    for _ in range(3):
        s.kill(20, 2)
    for _ in range(60):
        s.kill(21, 2)
    s.idle()
    assert s.k._U >= 23
    assert s.k.total <= s.real == 67


def _drop_digit(v, rnd):
    s = str(v)
    if len(s) < 2:
        return v
    i = rnd.randrange(len(s))
    return int(s[:i] + s[i + 1:])


def probe(seed, misreads=True):
    """A random party session: 1-3 mobs, dropped and misread lines, identical
    screens, epoch boundaries and level-ups. -> (real kills, the most the count
    was ever above them)."""
    rnd = random.Random(seed)
    k = BarKills()
    n = rnd.randint(1, 3)
    base = rnd.choice([20, 40, 100, 200, 900])
    mobs = [max(2, int(base * rnd.uniform(0.85, 1.25))) for _ in range(n)]
    ratio = rnd.choice([0.05, 0.1, 0.1, 0.2])
    p_kdrop = rnd.choice([0, 0.1, 0.3])
    p_bdrop = rnd.choice([0, 0.1, 0.4, 0.8])
    p_ident = rnd.choice([0, 0.5, 0.9])  # the same mob again: its lines aren't new
    p_mis = rnd.choice([0, 0.02, 0.1]) * misreads  # a digit dropped from a value read
    p_bound = rnd.choice([0, 0.01, 0.05])
    p_wrap = rnd.choice([0, 0.005])
    drift = rnd.choice([1.0, 1.0, 1.1])  # the share grows at a level-up
    streak = rnd.choice([1, 5, 20])
    gap = rnd.choice([(0.3, 1.0), (1, 3), (2, 6)])
    t = 0.0
    gain = real = peak = pending = 0
    cur, left, prev = rnd.randrange(len(mobs)), streak, None
    next_kill, next_bar, shown_until = rnd.uniform(*gap), 0.0, -1
    while t < 400:
        t = round(t + 0.2, 3)
        if next_kill <= t < 380:
            next_kill = t + rnd.uniform(*gap)
            left -= 1
            if left <= 0:
                cur, left = rnd.randrange(len(mobs)), rnd.randint(1, streak)
            exp = mobs[cur]
            bonus = max(1, round(exp * ratio))
            real += 1
            pending += exp + bonus
            same, prev = prev == cur, cur
            shown_until = t + 8
            if not (same and rnd.random() < p_ident):
                kr, br = rnd.random() >= p_kdrop, rnd.random() >= p_bdrop
                kv = _drop_digit(exp, rnd) if rnd.random() < p_mis else exp
                bv = _drop_digit(bonus, rnd) if rnd.random() < p_mis else bonus
                if kr:
                    k.line(t, kv)
                    if br:
                        k.bonus(bv)
                        k.pair(kv + bv, bv)
                elif br:
                    k.bonus(bv)
        if t <= shown_until:
            k.shown(t)
        if rnd.random() < p_bound:
            k.boundary()
        if t >= next_bar:
            next_bar = t + 1.0
            wrap = bool(pending) and rnd.random() < p_wrap
            if wrap:
                mobs = [max(2, int(m * drift)) for m in mobs]
            gain += pending
            pending = 0
            k.update(gain, t, party=True, wrap=wrap)
        peak = max(peak, k.total - real)
    return real, peak


def test_misread_pair_of_a_known_mob_does_not_lower_the_epoch_floor():
    # Seed 497: "1064"/"106" read as "64"/"16" made a plausible pair of 80 that
    # dropped the too-low threshold for the epoch.
    assert probe(497)[1] == 0


def test_bonus_missed_at_session_start_seed_950():
    assert probe(950, misreads=False)[1] == 0


@pytest.mark.parametrize("misreads", [True, False])
def test_random_sessions_never_overcount(misreads):
    over = [seed for seed in range(300) if probe(seed, misreads)[1] > 0]
    assert over == []
