"""Round-3 review of the position-based ExpTracker: the reviewer's scenario
scripts s1-s6 and one test per finding."""

import json
import random

from mctracker.capture import parse_status_text
from mctracker.exp_table import EXP_TABLE as T
from mctracker.exp_table import bar_percent as bp
from mctracker.exp_tracker import CONTRADICT_CONFIRM as SUSPECT_CONFIRM, ExpTracker, Reading
from mctracker.kills import Mob
from mctracker.overlay import level_eta
from mctracker.session import Session, Tracker

N90 = 11_938_059  # level 90 isn't in the shipped table
N91 = 25_069_923
MOBS = [Mob(1, "x", 10)]


def R(t, level, exp, pct="auto", size=None):
    """Reading with ``level`` as the digits and the percent of ``size`` (default: the table's)."""
    if pct == "auto":
        pct = bp(exp, size or T[level])
    return Reading(t, level, exp, pct)


def run(seq, tr=None):
    tr = tr or ExpTracker()
    for i, (lvl, exp, *pct) in enumerate(seq):
        tr.add(R(i, lvl, exp, *pct))
    return tr


def tracker(tmp_path):
    tr = Tracker(save_dir=tmp_path)
    tr.on_map("Field", MOBS)
    return tr


def saved(tr):
    return json.loads(tr.saved[-1].read_text(encoding="utf-8"))


# --- s1: start, death after a step, flicker -------------------------------------


def test_s1a_unreadable_first_reading_is_never_the_start():
    t = run([(45, 14_473, None), (45, 74_500), (45, 74_600)])
    assert t.gained == 100 and t.levels_gained == 0


def test_s1a2_first_reading_misread_as_a_higher_level():
    # Rule 3 replaces the young-anchor re-anchor: no fake level, no inflation;
    # once 5 distinct readings contradict the anchor it moves, booking nothing.
    t = run([(46, 74_473), (45, 74_500), (45, 74_600)])
    assert t.gained == 0 and t.levels_gained == 0
    for i in range(3):
        t.add(R(10 + i, 45, 74_700 + 100 * i))
    assert t.level == 45 and t.levels_gained == 0 and not t.behind
    g = t.gained
    assert g <= 75_000 - 74_473  # re-anchored (L44 is a rise candidate, so sooner): undercount
    t.add(R(20, 45, 75_000))
    assert t.gained == g + 100


def test_s1a3_first_reading_misread_as_a_lower_level():
    # Rule 3 replaces the young-anchor re-anchor: no fake level, no inflation;
    # once 5 distinct readings contradict the anchor it moves, booking nothing.
    t = run([(44, 74_473), (45, 74_500), (45, 74_600)])
    assert t.gained == 0 and t.levels_gained == 0
    for i in range(3):
        t.add(R(10 + i, 45, 74_700 + 100 * i))
    assert t.level == 45 and t.levels_gained == 0 and not t.behind
    g = t.gained
    assert g <= 75_000 - 74_473  # re-anchored (L44 is a rise candidate, so sooner): undercount
    t.add(R(20, 45, 75_000))
    assert t.gained == g + 100


def test_s1b_low_level_kill_then_death():
    # Finding 3: the 300 EXP kill is real; the drop after it is a death.
    t = run([(10, 500), (10, 500), (10, 800), (10, 629), (10, 629), (10, 629), (10, 629)])
    # Round 6: the drop follows the still-young kill, so it is judged from the
    # kill's base: a rise of 129, not a death. Net exact; gained and lost lower.
    assert t.gained == 129 and t.lost == 0 and t.net == 129


def test_s1c_quest_reward_then_death():
    t = run([(45, 100_000), (45, 100_000), (45, 200_000), (45, 153_155), (45, 153_155), (45, 153_155), (45, 153_155)])
    # The quest (+21% in one reading) is never booked, so neither is the death
    # measured from it: undercount only (module doc, rule 2).
    assert t.gained == 0 and t.lost == 0


def test_s1d_low_digit_flicker_is_not_a_death():
    t = run([(45, 74_450), (45, 74_478), (45, 74_473), (45, 74_473), (45, 74_473), (45, 74_478), (45, 74_478)])
    assert t.gained == 28 and t.lost == 0


# --- s2: misread that fits a lower level ------------------------------------------


def test_s2_single_glyph_misreads_rarely_fit_another_level_uniquely():
    tr = ExpTracker()
    rnd = random.Random(1)
    conf = {"0": "86", "1": "7", "3": "8", "5": "6", "6": "58", "7": "1", "8": "306", "9": "8"}
    tot = wrong = 0
    for _ in range(300):
        lvl = rnd.randint(20, 89)
        n = T[lvl]
        e = rnd.randint(n // 50, n - 1)
        p = bp(e, n)
        for which, s in (("p", f"{p:.2f}"), ("e", str(e))):
            for i, c in enumerate(s):
                for d in conf.get(c, ""):
                    s2 = s[:i] + d + s[i + 1:]
                    r = Reading(0, lvl, e, float(s2)) if which == "p" else Reading(0, lvl, int(s2), p)
                    tot += 1
                    f = tr.fits(r)
                    wrong += len(f) == 1 and f[0] != lvl
    assert wrong / tot < 0.02


def test_s2e_lower_level_misread_does_not_wreck_best_minute():
    # Finding 4: a unique fit at a lower level is held as a suspect, not committed.
    s = Session()
    e = 100_000
    for i in range(300):  # 5 minutes at ~1000 EXP/min
        e += 17
        s.on_exp(45, e, bp(e, T[45]), now=i)
    assert s.exp.fits(Reading(0, 44, e, bp(e, T[44]))) == [44]
    s.on_exp(44, e, bp(e, T[44]), now=300)
    assert s.exp.behind  # suspect pending
    for i in range(301, 305):
        e += 17
        s.on_exp(45, e, bp(e, T[45]), now=i)
    assert s.best_minute() <= 1100
    assert s.exp.gained == e - 100_017  # first reading is the anchor
    assert not s.exp.behind


def test_persistent_big_drop_re_anchors_without_a_loss():
    t = run([(45, 100_000), (45, 100_100), (45, 100_200)])
    for i in range(SUSPECT_CONFIRM):
        t.add(R(10 + i, 45, 10_000 + i))  # -19% of the level: not a death
        if i < SUSPECT_CONFIRM - 1:
            assert t.gained == 200  # never committed straight away
    # Round 6: a downward re-anchor takes back what was booked above the new
    # position (here all of it: the chain is below the anchor).
    assert t.lost == 0 and t.gained == 0 and t.retracted == 200 and t.level == 45 and not t.behind
    t.add(R(20, 45, 10_100))
    assert t.gained == 100 - (SUSPECT_CONFIRM - 1)


# --- s3/s4/s6: level 90+ ----------------------------------------------------------


def l90(t, digits, exp, size=N90):
    t.add(R(t._clock, digits, exp, size=size))
    t._clock += 1


def start_at_89():
    t = ExpTracker()
    t._clock = 0
    l90(t, 89, 5_600_000, T[89])
    l90(t, 89, 5_680_000, T[89])
    return t


def test_s3_level_digit_misreads_at_level_90():
    t = start_at_89()
    e = 50_000
    for _ in range(55):
        e += 3000
        l90(t, 90, e)
    assert abs(t.learned[90] - N90) / N90 < 0.002 and t.level == 90
    for i in range(200):  # every 10th reading shows "80"
        e += 3000
        l90(t, 80 if i % 10 == 0 else 90, e)
    assert t.distrusted == set() and t.warnings == []
    g = t.gained
    for _ in range(30):
        e += 3000
        l90(t, 90, e)
    assert t.gained - g == 90_000
    assert t.gained == T[89] - 5_600_000 + e


def test_s4_level_90_then_91_books_everything():
    t = start_at_89()
    e, stall, longest = 50_000, 0, 0
    while e < N90 - 30_000:
        e += 30_000
        before = t.last
        l90(t, 90, e)
        stall = stall + 1 if t.last is before else 0
        longest = max(longest, stall)
    assert longest <= 15  # only while level 90's size is learned
    e = 200_000
    for _ in range(100):
        e += 30_000
        l90(t, 91, e, N91)
    true = (T[89] - 5_600_000) + N90 + e
    assert t.levels_gained == 2
    assert abs(true - t.gained) < N90 * 0.002
    assert abs(t.learned[91] - N91) / N91 < 0.002


def test_s6_level_90_read_as_9_keeps_counting():
    # Finding 6: never distrust or relearn level 9; learn 90, the missing neighbour.
    t = start_at_89()
    e = 50_000
    for _ in range(300):
        e += 30_000
        l90(t, 9, e)
    assert t.distrusted == set() and t.warnings == [] and 9 not in t.learned
    assert t.need(9) == T[9]
    assert abs(t.learned[90] - N90) / N90 < 0.002
    assert t.gained == T[89] - 5_600_000 + e
    assert t.level == 90 and t.exp_remaining() is not None


def test_distrust_needs_the_exact_digits():
    # Readings that fit no level, with digits off by one: never overrule level 31.
    t = run([(30, 50_000)])
    for i in range(60):
        exp = 40_000 + 137 * i
        t.add(Reading(1 + i, 32, exp, bp(exp, 100_000)))
    assert t.distrusted == set() and t.learned == {}


# --- s5: session lifecycle ----------------------------------------------------------


def test_s5f_death_to_zero_then_town_is_not_a_level_up(tmp_path):
    # Finding 1: 0.00% fits every level; the digits say 45, so it's a death.
    tr = tracker(tmp_path)
    for i, e in enumerate([20_000, 21_000, 22_000, 23_000, 0, 0, 0, 0]):
        tr.on_status(45, e, bp(e, T[45]), now=i)
    tr.on_map("Town", [])
    d = saved(tr)
    assert (d["exp_gained"], d["exp_lost"], d["levels_gained"]) == (2000, 22_000, 0)  # round 6: the last kill before a death is judged from its base


def test_death_to_zero_with_unreadable_digits_prefers_the_committed_level():
    t = run([(45, 20_000), (45, 23_000)])
    for i in range(4):
        t.add(R(10 + i, 4, 0))  # digits misread: no tie-break
    # Only L45 is an acceptable move (a death to 0). Repeats are no evidence,
    # so the dip waits; the session end books it. Round 6: the young 3000 kill
    # before it is taken back and the loss counted from its base.
    assert t.behind and t.lost == 0
    t.finalize()
    assert t.levels_gained == 0 and t.lost == 20_000 and t.gained == 0


def test_zero_percent_with_next_level_digits_from_a_low_bar_is_not_a_level_up():
    t = run([(45, 20_000), (45, 23_000)])
    for i in range(4):
        t.add(R(10 + i, 46, 0))  # a death could have emptied a 5% bar
    t.finalize()
    assert t.levels_gained == 0 and t.gained == 0  # round 6: the last kill before a death is judged from its base


def test_s5g_tail_the_percent_rejected_is_not_booked(tmp_path):
    # Finding 5: only unreadable-percent readings may form the end-of-session tail.
    tr = tracker(tmp_path)
    for i, e in enumerate([70_000, 74_000, 74_473]):
        tr.on_status(45, e, bp(e, T[45]), now=i)
    for i in range(3):
        tr.on_status(45, 79_473, 15.90, now=10 + i)  # fits no level
    tr.on_map("Town", [])
    assert saved(tr)["exp_gained"] == 4473


def test_s5h_misread_before_start_is_not_the_baseline(tmp_path):
    # Finding 2.
    tr = tracker(tmp_path)
    tr.on_status(45, 74_000, 15.80, now=0)
    tr.on_status(45, 14_473, 15.90, now=1)  # fits no level
    assert tr.session is None
    tr.on_status(45, 74_473, 15.90, now=2)
    for i in range(3):
        tr.on_status(45, 74_500 + i, bp(74_500 + i, T[45]), now=3 + i)
    assert tr.session.exp.gained == 502


def test_session_starts_only_on_a_validated_rise(tmp_path):
    tr = tracker(tmp_path)
    tr.on_status(45, 74_000, 15.80, now=0)
    tr.on_status(45, 79_473, 15.90, now=1)  # higher, but fits no level
    tr.on_status(45, 80_000, None, now=2)  # percent unreadable
    assert tr.session is None
    tr.on_status(45, 74_100, bp(74_100, T[45]), now=3)
    assert tr.session is not None and tr.session.exp.gained == 100


def test_start_waits_for_a_fit_the_digits_agree_with():
    t = ExpTracker()
    t.add(Reading(0, 45, 74_473, bp(74_473, T[46])))  # percent misread: fits 46, digits 45
    assert t.last is None and t.behind
    t.add(R(1, 45, 74_500))
    t.add(R(2, 45, 74_600))
    assert t.level == 45 and t.gained == 100 and t.levels_gained == 0


# --- finding 7: the committed level is what's shown and saved ---------------------------


def test_level_shown_and_saved_is_the_committed_level(tmp_path):
    tr = tracker(tmp_path)
    tr.on_status(45, 100_000, bp(100_000, T[45]), now=0)
    tr.on_status(45, 100_100, bp(100_100, T[45]), now=1)
    tr.on_status(4, 100_200, bp(100_200, T[45]), now=2)  # "45" read as "4"
    s = tr.session
    assert s.level == 45 and s.exp_percent == bp(100_200, T[45])
    assert level_eta(s).startswith("Lv 46 in")
    tr.on_map("Town", [])
    assert saved(tr)["level"] == 45


# --- interface for the kill counter -------------------------------------------------------


def test_verified_gain_behind_and_last_committed_t():
    # Round 4: a rise is verified only once two changed frames could no longer
    # take it back; until then it's in unconfirmed_gain.
    t = run([(45, 100_000), (45, 100_500)])
    assert t.verified_gain == 0 and t.unconfirmed_gain == 500 and not t.behind and t.last_committed_t == 1
    t.add(R(2, 45, 90_000))  # possible death: the young 500 is taken back, dip open
    assert t.behind and t.verified_gain == 0 and t.gained == 0 and t.last_committed_t == 1
    t.add(R(3, 45, 101_000))  # back up: no death; +1000 from 100_000, still young
    assert not t.behind and t.verified_gain == 0 and t.unconfirmed_gain == 1000 and t.last_committed_t == 3
    for i in range(3):
        t.add(R(4 + i, 45, 102_000, None))
    assert t.finalize() == 1000
    assert t.gained == 2000 and t.verified_gain == 1000  # the unreadable tail isn't verified


def test_behind_while_learning_a_missing_level():
    t = start_at_89()
    l90(t, 90, 100_000)
    assert t.behind and t.verified_gain + t.unconfirmed_gain == 80_000


# --- rounding and capture ----------------------------------------------------------------


def test_percent_ties_round_half_up():
    assert bp(1, 800) == 0.13  # 0.125 exactly; float round() gives 0.12
    assert bp(74_473, 468_450) == 15.90


def test_capture_keeps_exp_when_the_percent_is_unreadable():
    assert parse_status_text("EXP.66,700[14.24%]") == (66_700, 14.24)
    assert parse_status_text("EXP.66,700[1?.2") == (66_700, None)
    assert parse_status_text("EXP.66,70014.24%") is None  # no bracket: digits may be glued
