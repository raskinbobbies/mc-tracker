"""EXP needed to go from level L to L+1 in MapleStory Classic World.

Public game data, copied from two independent references that agree on every
level 1-89 except one typo:

* Hidden Street's EXP table (2004-2006, pre-Big Bang GMS):
  https://wayback.hidden-street.net/exptable.php
* henesys.gg "Formulas" guide, read from the Classic World launch client
  (2026-10-06): https://henesys.gg/guides/formulas

Level 59 is 1,147,132 (henesys.gg; Hidden Street has 1,147,032). From level 50
on, each entry is floor(previous * 1.0548), and only 1,147,132 fits that.

Levels 90-99 are left out: the references disagree there (Hidden Street keeps
the old ~5% curve, henesys.gg reports 2.1x per level). ``ExpTracker`` learns
missing levels from the bar instead.

Checked against this server's bar at level 45: "74,473 [15.90%]" and a
reference tracker's "74,069 / 468,450 -> 15.81%". The game ROUNDS the percent
to 2 decimals (74,473 / 468,450 = 15.898% shows as 15.90, not 15.89).
"""

from __future__ import annotations

_NEEDS = [
    15, 34, 57, 92, 135, 372, 560, 840, 1242, 1716,  # 1-10
    2360, 3216, 4200, 5460, 7050, 8840, 11040, 13716, 16680, 20216,  # 11-20
    24402, 28980, 34320, 40512, 47216, 54900, 63666, 73080, 83720, 95700,  # 21-30
    108480, 122760, 138666, 155540, 174216, 194832, 216600, 240500, 266682, 294216,  # 31-40
    324240, 356916, 391160, 428280, 468450, 510420, 555680, 604416, 655200, 709716,  # 41-50
    748608, 789631, 832902, 878545, 926689, 977471, 1031036, 1087536, 1147132, 1209994,  # 51-60
    1276301, 1346242, 1420016, 1497832, 1579913, 1666492, 1757815, 1854143, 1955750, 2062925,  # 61-70
    2175973, 2295216, 2420993, 2553663, 2693603, 2841212, 2996910, 3161140, 3334370, 3517093,  # 71-80
    3709829, 3913127, 4127566, 4353756, 4592341, 4844001, 5109452, 5389449, 5684790,  # 81-89
]

EXP_TABLE: dict[int, int] = {lvl: need for lvl, need in enumerate(_NEEDS, start=1)}


def bar_percent(exp: int, need: int) -> float:
    """The percent the game shows for ``exp`` into a level of size ``need``."""
    return round(exp * 100 / need, 2)
