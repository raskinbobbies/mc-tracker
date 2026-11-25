# Decisions

Architecture decisions, newest first. A decision that changes an older one says so; the older one gets a dated
"Changed:" line.

## 2026-11-25 — Party kills: event matching, not magnitude pricing

Supersedes the 2026-11-25 per-epoch formula (`lines + settled-unpaid-gain // U`, recorded in
`docs/handoff/2026-11-25.md`). Issue #22, PR #31.

**Context.** In a party each kill gives a share that varies with damage, plus a bonus line. The per-epoch formula
priced kills from OCR'd magnitudes: a line's cost, the epoch's largest pair `U`, its smallest pair, and `gain // U`
for kills whose lines were missed. It failed three opus reviews in a row, each finding new overcount paths (a bar step
from an unread bigger mob counted as `g // U`; a bonus read low before the real one was known made later lines cheap;
a pair with both lines misread lowered the floor; `pair()` accepted a kill value ≤ 0). Adversarial probes overcounted
on about 1% of seeds. A second opinion traced every path to one root: any misread that lowers a price becomes extra
kills. Patching each path added more state and more paths.

**Decision.** Party kills are counted from events only (`BarKills._party_update` / `_match`):

- **Step:** one rise of the trusted EXP bar, kept only if an EXP line was on screen within `nearby_s` (8 s) of it.
  Rises within `merge_s` (0.5 s) of a step's first rise, or while the bar is unsure, join that step. A level-up wrap is
  one step.
- **Line:** one new kill line. A line with no bonus line that repeats a value read within `repeat_s` (8 s) before it
  is *suspect* (maybe a re-read). So is a line `LineDiff` places late (`Message.late`): new above a message first
  read at least `late_s` (1 s) before, new in its slot after rows there were taken for a misread of the slot's old
  message for at least 1 s, or new in a frame where messages hidden by 3+ empty frames line up again. Every other
  line is *trusted*. A bonus line belongs to a kill line read at most `bonus_s` (0.5 s) before it.
- **M:** a maximum matching of lines (trusted or suspect) to steps at most `match_s` (L = 6 s) apart, one line per
  step, found by a greedy two-pointer over time and decided only once no later event can change it.
- **Count:** `trusted lines + steps − M`.

Line values are kept only to tell monsters apart (`KillCounter`). The magnitude machinery is gone: `_cost`, `_U`,
`_settled`, `_dropped`, `_screen_pair`, pair pricing, `per_kill`, the quarantine of unpriced gain, epochs.
`boundary()` and `pair()` stay as no-ops for callers. The solo path is unchanged.

**Invariant.** Each trusted line is a distinct real kill, and its kill lies in at most one step, within L of the
line. So the steps holding a trusted line's kill can be matched to distinct trusted lines: there are at most M of
them. Every other step holds at least one kill whose line is not trusted, and distinct steps hold distinct kills. So
`count ≤ trusted lines + (steps holding no trusted line's kill) ≤ real kills`. Dropping events and merging steps only
lower the count.

It rests on three assumptions:

1. A kept step holds kill EXP (the owner rules out quest EXP during a grind).
2. A kill line shows within L of its bar step. On the owner's 4-member recording (`tests/test_party_replay.py`) the
   largest offset is 1.8 s, and L below 1.5 s overcounts there (64 at 0.3 s). Bar rises there are never closer than
   1.0 s, so the 0.5 s merge window never fires.
   **Changed (2026-11-25): L = 2.5 s → 6 s, and late lines are suspect.** The full-pipeline sim (`SIM_STEP=0.212
   SIM_ROWS=6`, party 5-12 s) gave 201–206 for 200 real at L = 2.5 s: a new kill line takes the slot of a faded one
   above an identical bonus line, so `LineDiff` takes it for a misread of the old line until a frame shows it alone,
   or until the old slot expires (`keep_items` frames, 6.4 s at 0.212 s frames). L = 4 s and 5 s still overcounted;
   6 s was the only clean value. Late lines are the margin past 6 s: with the late rule every sim line read more than
   2.2 s after its kill is late (the largest was 6.36 s), and `test_a_kill_line_read_seconds_after_its_step_counts_once`
   double counts without it. With both, no party sim row exceeds real kills on 6 seeds, and `r3_probe.py` and
   `probe_events` stay at 0 overcounting seeds in 5,000.
3. A trusted line is never a re-read. A re-read of a kill line alone is suspect. A kill line and its bonus line both
   read again as new are not covered and rely on `LineDiff`.

**Accepted costs.**

- Kills that share one bar step count once unless their lines were read: AoE and multi-kill frames lose recall.
- A suspect line counts nothing. On a one-mob map with bonus lines missed, many real kills are suspect.
- Kills settle about L (6 s) after their line.
- A late line counts nothing when its step is matched to it: 4–5% of kills on the party 5-12 s sim (191 of 200
  against 200 without the late rule), and the replay drops from 47 to 42 of 53 (L = 6 s alone: 45; the late rule:
  2 lines after a 3-frame gap that may be re-reads, 1 real kill read in a rejoin frame).
- A quest's EXP line during a grind is a trusted line (assumption 1): the sim row "party quest w/ EXP line 60 /10"
  counts 219/217 for 200 kills, as on main.
