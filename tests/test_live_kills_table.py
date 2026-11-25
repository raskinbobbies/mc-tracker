"""Live monsters table in a party (#27), and mesos at the start of a session."""

import os
import random

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from mctracker.exp_table import EXP_TABLE, bar_percent  # noqa: E402
from mctracker.gui import MonsterTable, unknown_kills_text  # noqa: E402
from mctracker.kills import Mob, parse_message  # noqa: E402
from mctracker.session import Tracker  # noqa: E402

BASE = 100_000


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class NoAssets:
    def mob_sprite(self, mob_id, cb):
        pass


def _status(tr, exp, now):
    tr.on_status(45, exp, bar_percent(exp, EXP_TABLE[45]), now=now)


def party_session(tmp_path, mobs, f, kills=40, stray_first_line=False, seed=1):
    """A party session on a map with ``mobs``: each kill line is a share ``f`` of the
    monster's EXP (+-25%), with its bonus line, then the bar moves. Returns the session
    after the bar has had time to settle. ``stray_first_line``: the line that starts the
    session (a mid-sized kill, which alone fits the wrong scale) is flagged solo, as when
    its bonus line is missed."""
    rnd = random.Random(seed)
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Forest Up North", mobs)
    tr.party_size = 2
    bar = BASE
    _status(tr, bar, 0)
    _status(tr, bar, 0.5)
    first = int(mobs[len(mobs) // 2].exp * f)
    tr.on_message_lines([(f"You received EXP (+{first})", 800)], now=1)
    s = tr.session
    s._bonus_t = None
    s.killer.solo = False
    s.killer.party[0] = not stray_first_line  # else it stays flagged solo
    now = 2.0
    for i in range(kills):
        share = int(mobs[i % len(mobs)].exp * f * rnd.uniform(0.75, 1.25))
        bonus = max(1, share // 10)
        msgs = [parse_message(f"You received EXP (+{share})"), parse_message(f"Bonus EXP for PARTY (+{bonus})")]
        for m in msgs:
            s.on_message(m, now=now)
        s.bar.shown(now)
        s.on_lines(msgs, now=now)
        bar += share + (bonus if len(msgs) == 2 else 0)
        _status(tr, bar, now + 0.3)
        _status(tr, bar, now + 0.6)
        s.on_lines([], now=now + 1)
        now += 3
    _status(tr, bar, now + 20)
    s.on_lines([parse_message("You received EXP (+1)")], now=now + 25)
    return s


def table_counts(table, mobs):
    return {m.name: table.item(r, 4).text() for r, m in enumerate(mobs)}


def test_party_live_table_shows_each_mob_when_the_session_has_counts(app, tmp_path):
    mobs = [Mob(1, "Small", 400), Mob(2, "Medium", 1000), Mob(3, "Big", 3000)]
    s = party_session(tmp_path, mobs, f=0.35, stray_first_line=True)
    assert s.party_seen and s.total_kills > 30
    table = MonsterTable(NoAssets())
    table.show_mobs(s.mobs, s.kills, s.active_s)
    shown = {name: int(n) for name, n in table_counts(table, mobs).items()}
    assert all(n > 0 for n in shown.values()), shown  # was Big: 0 (and 15 unknown)
    assert sum(shown.values()) <= s.total_kills  # undercount only
    assert s.unassigned_kills <= 2


def test_one_stray_solo_line_does_not_decide_the_party_scale(tmp_path):
    mobs = [Mob(1, "Small", 400), Mob(2, "Medium", 1000), Mob(3, "Big", 3000)]
    s = party_session(tmp_path, mobs, f=0.35, stray_first_line=True)
    assert s.killer.party.count(False) == 1
    s.killer.classify(1)  # fit
    assert 0.25 < s.killer._party_fit[0] < 0.5  # party shares have their own scale


def test_party_on_close_mobs_stays_unassigned_never_guessed(app, tmp_path):
    # Shares vary too much to tell monsters worth 54 and 63 EXP apart (#26/#27's
    # real map, Forest Up North VI): nothing is guessed, the footer says so.
    mobs = [Mob(27, "Fairy 3", 54), Mob(31, "Curse Eye", 63)]
    s = party_session(tmp_path, mobs, f=0.3)
    assert not s.killer.party_identifiable()
    assert sum(s.kills.values()) == 0
    assert s.unassigned_kills == s.total_kills > 30
    assert unknown_kills_text(s.unassigned_kills, s.party_seen).endswith("unknown shared kills")


def test_party_on_a_single_mob_map_counts_every_kill(app, tmp_path):
    mobs = [Mob(31, "Curse Eye", 63)]
    s = party_session(tmp_path, mobs, f=0.3)
    table = MonsterTable(NoAssets())
    table.show_mobs(s.mobs, s.kills, s.active_s)
    assert int(table.item(0, 4).text()) == s.total_kills > 30


# --- mesos at the start of a session ------------------------------------------------


def test_mesos_count_from_the_first_line_after_the_session_starts(tmp_path):
    # Solo video of #27: the first mesos line appeared ~25 s in, the app showed it at
    # once. Nothing is held back until a warm-up: 0 before the first pickup is real.
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Forest Up North VI", [Mob(31, "Curse Eye", 63)])
    _status(tr, BASE, 0)
    tr.on_message_lines([("You received EXP (+63)", 880)], now=1)
    assert tr.session is not None and tr.session.mesos == 0
    _status(tr, BASE + 63, 2)
    tr.on_message_lines([("You received EXP (+63)", 858), ("You have gained mesos (+77)", 880)], now=25)
    assert tr.session.mesos == 77


def test_mesos_read_before_the_session_starts_are_not_counted(tmp_path):
    # Old lines already on screen when the tracker attaches must not leak into a session.
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Forest Up North VI", [Mob(31, "Curse Eye", 63)])
    _status(tr, BASE, 0)
    tr.on_message_lines([("You have gained mesos (+500)", 858), ("You received EXP (+63)", 880)], now=1)
    assert tr.session is not None and tr.session.mesos == 0


# --- never more than the truth per mob (undercount only) ------------------------------


def _over_count(mobs, solo, shares, party_seen=True):
    """Lines (value, mob id): ``solo`` read as solo kills, then party ``shares``.
    -> the most any mob is over-counted by ``identify`` (0: never more than its real kills)."""
    from collections import Counter

    from mctracker.kills import KillCounter

    kc = KillCounter()
    kc.set_map(mobs)
    for v, _ in solo:
        kc.add(v)
    for v, _ in shares:
        kc.add(v, party=True)
    truth = Counter(m for _, m in solo + shares)
    got = Counter(kc.identify(v, False) for v, _ in solo) + Counter(kc.identify(v, True) for v, _ in shares)
    got.pop(None, None)
    return max([got[m] - truth[m] for m in got], default=0)


def test_stray_solo_line_never_overcounts_the_biggest_mob():
    mobs = [Mob(1, "A", 54), Mob(2, "B", 63), Mob(3, "C", 130)]
    solo = [(63, 2)]
    shares = [(56, 1)] * 10 + [(43, 1)] * 10 + [(78, 3)] * 10
    assert _over_count(mobs, solo, shares) <= 0
    from mctracker.kills import KillCounter

    kc = KillCounter()
    kc.set_map(mobs)
    kc.add(63)
    for v, _ in shares:
        kc.add(v, party=True)
    assert sum(kc.identify(v, True) == 3 for v, _ in shares) <= 10


@pytest.mark.parametrize("seed", range(20))
def test_party_lines_never_overcount_any_mob_with_a_stray_solo_line(seed):
    rnd = random.Random(seed)
    mobs = [Mob(1, "A", 54), Mob(2, "B", 63), Mob(3, "C", 130)]
    f = rnd.choice((0.3, 0.45, 0.6))
    shares = []
    for _ in range(30):
        m = rnd.choice(mobs)
        shares.append((int(m.exp * f * rnd.uniform(0.75, 1.25)), m.id))
    solo = [(rnd.choice(mobs).exp, 0)]  # stray solo line (mob id irrelevant: not counted below)
    solo = [(v, next(m.id for m in mobs if m.exp == v)) for v, _ in solo]
    assert _over_count(mobs, solo, shares) <= 0


@pytest.mark.parametrize("seed", range(20))
def test_party_shares_after_real_solo_kills_never_overcount(seed):
    # Three real solo kills fit the solo scale (1.0); the party's shares must not be
    # read on it (each mob would get the next mob's shares).
    rnd = random.Random(seed)
    mobs = [Mob(1, "A", 400), Mob(2, "B", 1000), Mob(3, "C", 3000)]
    f = rnd.choice((0.2, 0.35, 0.5))
    solo = [(m.exp, m.id) for m in mobs]
    shares = [(int(m.exp * f * rnd.uniform(0.8, 1.2)), m.id) for m in mobs for _ in range(10)]
    assert _over_count(mobs, solo, shares) <= 0
    from mctracker.kills import KillCounter

    kc = KillCounter()
    kc.set_map(mobs)
    for v, _ in solo:
        kc.add(v)
    for v, _ in shares:
        kc.add(v, party=True)
    # Far apart monsters: a share is its own monster's or unassigned, never another's.
    assert all(kc.identify(v, True) in (m, None) for v, m in shares)
