"""PR #31 last round: probe seeds pinned so any change to party counting shows here.

The fixed seeds must stay at 0 over real kills. The three accepted residuals (see the r8
"Changed:" entry in docs/decisions.md) may pass real kills by one, never more; if one of
them drops to 0, tighten its assert and update the decision record."""

import pytest

import party_probe as P


@pytest.mark.parametrize("run, gen, seed, mis", [
    (P.run_x, "barlow2", 243, 0),  # low bar for 2+ readings, its line read 0.6 s after the rise
    (P.run_x, "barlow2", 619, 0),  # a 57 EXP correction of a 1,518 step
    (P.run_x, "barlow2", 0, 1),
    (P.run_x, "barlow2", 4, 1),  # 8 over before the barlow2 merge rule
    (P.run_x, "loot", 445, 1),  # one misread row at a new place after a frame with messages
])
def test_fixed_probe_seeds_never_overcount(run, gen, seed, mis):
    real, total, peak = run(seed, gen, mis=mis)
    assert peak == 0, (real, total)


@pytest.mark.parametrize("gen, seed", [
    # One kill read twice as kill + bonus, with identical pairs in harsh-misread frames
    # (assumption 3's uncovered case).
    ("barlow", 499),
    ("barlow", 2588),
    # A dropped row before a read-failure burst, then a misread bonus 6 s to fade after its kill.
    ("gapread", 3049),
])
def test_accepted_residual_seeds_overcount_by_one_at_most(gen, seed):
    real, total, peak = P.run_r6(seed, gen, mis=1.0)
    assert peak <= 1, (real, total, peak)  # known residual: +1 now; more is a regression
    assert total <= real  # the +1 is transient: by the end the total is under real kills
