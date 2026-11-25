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
    assert 0.25 < s.killer.scale < 0.5  # the party's share, not the one solo line's


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
