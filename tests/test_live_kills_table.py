"""Live monsters table in a party (#27), and mesos at the start of a session."""

import os
import random
from collections import Counter

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


def party_session(tmp_path, mobs, f, kills=40, stray_first_line=False, seed=1, strays=0):
    """A party session on a map with ``mobs``: each kill line is a share of the monster's
    EXP, with its bonus line, then the bar moves. ``f``: the share, a fraction (+-25%
    per kill, mobs in turn) or a ``(lo, hi)`` range drawn per kill (random mobs).
    Returns ``(session, truth)``, truth being the real kills per mob id, after the bar
    has had time to settle.
    ``stray_first_line``: the line that starts the session (a mid-sized kill, which alone
    fits the wrong scale) is flagged solo, as when its bonus line is missed.
    ``strays``: that many lines in the session come with no bonus line and are flagged
    solo: the first is a misread share, the others real solo kills at full value."""
    rnd = random.Random(seed)

    def share_of(m):
        k = rnd.uniform(*f) if isinstance(f, tuple) else f * rnd.uniform(0.75, 1.25)
        return max(1, int(m.exp * k))

    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Forest Up North", mobs)
    tr.party_size = 2
    bar = BASE
    _status(tr, bar, 0)
    _status(tr, bar, 0.5)
    mid = mobs[len(mobs) // 2]
    first = share_of(mid)
    truth = Counter({mid.id: 1})
    tr.on_message_lines([(f"You received EXP (+{first})", 800)], now=1)
    s = tr.session
    s._bonus_t = None
    s.killer.solo = False
    s.killer.party[0] = not stray_first_line  # else it stays flagged solo
    stray_at = sorted(rnd.sample(range(kills), strays))
    now = 2.0
    for i in range(kills):
        m = rnd.choice(mobs) if isinstance(f, tuple) else mobs[i % len(mobs)]
        truth[m.id] += 1
        stray = i in stray_at
        if stray:
            share = share_of(m) if i == stray_at[0] else m.exp  # misread share / solo kill
            msgs = [parse_message(f"You received EXP (+{share})")]
            s._bonus_t = None  # no bonus line around it: read as a solo kill
        else:
            share = share_of(m)
            bonus = max(1, share // 10)
            msgs = [parse_message(f"You received EXP (+{share})"), parse_message(f"Bonus EXP for PARTY (+{bonus})")]
        for msg in msgs:
            s.on_message(msg, now=now)
        if stray:
            assert not s.killer.party[-1]
        s.bar.shown(now)
        s.on_lines(msgs, now=now)
        bar += share + (bonus if len(msgs) == 2 else 0)
        _status(tr, bar, now + 0.3)
        _status(tr, bar, now + 0.6)
        s.on_lines([], now=now + 1)
        now += 3
    _status(tr, bar, now + 20)
    s.on_lines([parse_message("You received EXP (+1)")], now=now + 25)
    return s, truth


def table_counts(table, mobs):
    return {m.name: table.item(r, 4).text() for r, m in enumerate(mobs)}


def test_party_live_table_shows_the_top_mob_and_never_overcounts(app, tmp_path):
    # Party shares are never classified mob by mob (#27): only the top mob's shares
    # that no other mob could give are counted, the rest stay unknown shared kills.
    mobs = [Mob(1, "Small", 400), Mob(2, "Medium", 1000), Mob(3, "Big", 3000)]
    # f=0.85: Big's shares often clear Medium's at a 2x event (the window's floor).
    s, truth = party_session(tmp_path, mobs, f=0.85, stray_first_line=True)
    assert s.party_seen and s.total_kills > 30
    table = MonsterTable(NoAssets())
    table.show_mobs(s.mobs, s.kills, s.active_s)
    shown = {m.id: int(n) for m, n in zip(mobs, table_counts(table, mobs).values())}
    assert shown[3] > 0, shown
    assert all(shown[m] <= truth[m] for m in shown), (shown, truth)  # undercount only
    # The estimate is spread by what was identified (here the top mob only): it adds up.
    est = s.kills_estimate_by_mob
    assert set(est) == {3} and est[3]["total"] == s.kills_estimate["total"] >= s.total_kills
    assert s.unassigned_kills == 0
    table.show_mobs(s.mobs, s.kills, s.active_s, est)
    assert table.cellWidget(2, 4).text().startswith("<div align='right'>~")


def test_one_stray_solo_line_does_not_decide_the_party_window(tmp_path):
    mobs = [Mob(1, "Small", 400), Mob(2, "Medium", 1000), Mob(3, "Big", 3000)]
    s, truth = party_session(tmp_path, mobs, f=0.35, stray_first_line=True)
    assert s.killer.party.count(False) == 1
    s.killer.counts()  # fit
    assert s.killer._solo_lines < s.killer.min_solo_fit  # so the window starts at 1x
    assert all(s.kills[m] <= truth[m] for m in truth), (s.kills, truth)


def test_party_on_close_mobs_stays_unassigned_never_guessed(app, tmp_path):
    # Shares vary too much to tell monsters worth 54 and 63 EXP apart (#26/#27's
    # real map, Forest Up North VI): nothing is guessed, the footer says so.
    mobs = [Mob(27, "Fairy 3", 54), Mob(31, "Curse Eye", 63)]
    s, _ = party_session(tmp_path, mobs, f=0.3)
    assert sum(s.kills.values()) == 0
    # No mob identified and no spawn mix: the whole estimate stays unassigned.
    assert s.kills_estimate_by_mob == {}
    assert s.unassigned_kills == s.kills_estimate["total"] >= s.total_kills > 30
    assert unknown_kills_text(s.unassigned_kills, s.party_seen).endswith("unknown shared kills")
    assert unknown_kills_text(s.unassigned_kills, s.party_seen, s.total_kills).endswith("unknown shared kills")


def test_party_on_a_single_mob_map_counts_every_kill(app, tmp_path):
    mobs = [Mob(31, "Curse Eye", 63)]
    s, _ = party_session(tmp_path, mobs, f=0.3)
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
    tr.on_message_lines([("You received EXP (+63)", 858), ("You have gained mesos (+77)", 880)], now=25.15)
    assert tr.session.mesos == 77  # booked on its second read, ~0.15 s later


def test_mesos_read_before_the_session_starts_are_not_counted(tmp_path):
    # Old lines already on screen when the tracker attaches must not leak into a session.
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Forest Up North VI", [Mob(31, "Curse Eye", 63)])
    _status(tr, BASE, 0)
    tr.on_message_lines([("You have gained mesos (+500)", 858), ("You received EXP (+63)", 880)], now=1)
    assert tr.session is not None and tr.session.mesos == 0


# --- never more than the truth per mob (undercount only) ------------------------------


MAPS = [
    [Mob(1, "A", 400), Mob(2, "B", 1000), Mob(3, "C", 3000)],
    [Mob(1, "A", 100), Mob(2, "B", 210), Mob(3, "C", 450)],
    [Mob(1, "A", 300), Mob(2, "B", 700)],
    [Mob(1, "A", 54), Mob(2, "B", 63), Mob(3, "C", 130)],
]


@pytest.mark.parametrize("mobs", MAPS, ids=lambda ms: "/".join(str(m.exp) for m in ms))
@pytest.mark.parametrize("seed", range(25))
def test_party_session_never_overcounts_any_mob(tmp_path, mobs, seed):
    # Shares drawn 0.3-1.0 of the kill, 0-3 lines flagged solo (the first a misread
    # share): no mob ever shows more kills than it really had.
    strays = random.Random(seed).randint(0, 3)
    s, truth = party_session(tmp_path, mobs, f=(0.3, 1.0), seed=seed, strays=strays)
    assert all(s.kills[m.id] <= truth[m.id] for m in mobs), (s.kills, truth, strays)


def _killer(mobs, solo, shares):
    from mctracker.kills import KillCounter

    kc = KillCounter()
    kc.set_map(mobs)
    for v in solo:
        kc.add(v)
    for v in shares:
        kc.add(v, party=True)
    return kc


def test_stray_solo_line_never_overcounts_the_biggest_mob():
    # Repro: one stray 35 fits a 0.65x solo scale, which put the window at 42..88 and
    # counted the 63-mob's shares as the 130-mob (14 vs a true 10).
    mobs = [Mob(1, "A", 54), Mob(2, "B", 63), Mob(3, "C", 130)]
    rnd = random.Random(0)
    shares = [int(63 * rnd.uniform(0.7, 1.0)) for _ in range(10)]
    shares += [int(130 * rnd.uniform(0.5, 1.0)) for _ in range(10)]
    kc = _killer(mobs, [35], shares)
    assert all(kc.identify(v, True) is None for v in shares[:10])
    assert sum(kc.identify(v, True) == 3 for v in shares) <= 10


def test_party_shares_after_real_solo_kills_count_only_in_the_top_mob_window():
    # Real solo kills (each mob twice) fit the solo scale (1.0): it places the window,
    # but shares of the lower mobs are never classified on it.
    mobs = [Mob(1, "A", 400), Mob(2, "B", 1000), Mob(3, "C", 3000)]
    rnd = random.Random(3)
    shares = [(int(m.exp * rnd.uniform(0.3, 1.0)), m.id) for m in mobs for _ in range(10)]
    kc = _killer(mobs, [m.exp for m in mobs] * 2, [v for v, _ in shares])
    assert all(kc.identify(v, True) in ((3, None) if m == 3 else (None,)) for v, m in shares)
    assert kc.counts()[1] == kc.counts()[2] == 2  # the solo kills


def test_party_estimate_spreads_only_over_this_maps_mobs(tmp_path):
    # A kill booked to a mob not on this map (or to None) is no weight: the spread uses the
    # map's own mobs, so no estimate lands on a row the table never shows.
    mobs = [Mob(1, "Small", 400), Mob(2, "Medium", 1000), Mob(3, "Big", 3000)]
    s, _ = party_session(tmp_path, mobs, f=0.85, stray_first_line=True)
    s.kills[99] += 5
    s.kills[None] += 2
    est = s.kills_estimate_by_mob
    assert set(est) <= {1, 2, 3} and sum(e["total"] for e in est.values()) == s.kills_estimate["total"]
