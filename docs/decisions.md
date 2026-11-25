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
   **Changed (2026-11-25, r4): enforced in `LineDiff`.** The r4 full-pipeline probe (message area → `LineDiff`, bar →
   `ExpTracker`) showed one kill making two trusted lines. `LineDiff` now returns a new message *late* when it sits
   in a slot whose different message was seen within `keep` frames (a misread of it, H1), when it was first taken as
   that slot's misread, when hidden messages lined up after 3+ empty frames even too few to rejoin (H2), or when
   another scroll lines up as many messages another way in a frame with a misread row. It *doubts* a message
   (`Message.doubt`, read by `BarKills`) whose slot is read otherwise before it was read the same way twice more. A
   misread row no longer marks its slot's message gone. All of these only demote lines to suspect.

**Step settling (2026-11-25, r4).** One kill also made two steps (H3: a bar read a little low, then true; H4: a
spike the tracker takes back). Party steps now come from `ExpTracker.current_gain` (the booked sum without the
high-water mark, so it falls when a spike is taken back) and a fall takes EXP back out of the steps still at the gate.
A rise joins the step before it if that step was seen in one bar reading only (at most `join_reads` = 2 such joins),
or if the bar went unsure while it waited. A step leaves the gate only once the tracker confirmed its gain or its high
was read twice, and after an unsure bar only after two sure readings. These only merge or drop steps.

**Probe (r4, 1,000 seeds, MIS off/on), over-count seeds:** base 0/0, mis1 0/0, barlow 0/0, gapread 0/3,
barspike 0/1 (from 0/241, 0/1020, 2431/2902, 1824/1851, 4071/4025 at 5,000 seeds). **Not yet 0:** gapread MIS=1 seeds
325, 650, 776 and barspike MIS=1 seed 443 (`r4_probe.py`). **Recall cost:** the replay counts 21 of 53 (was 42).

**Changed (2026-11-25, r6): four more demote/merge rules; steps are intervals.** Traced from the r4 residuals and
two new generators (`susp`, `suspfast`: one mob, 70% of bonus lines missed, with bar lows, spikes and read failures).

- A step the bar took EXP back from waits for two readings at its high (a spike, then a lower spike the tracker keeps).
- If the bar takes back more than the gate holds, the newest undecided step is dropped (a spike read twice had left
  the gate).
- A step is the span of its rises: a line matches it from L before its first rise to L after its last. A kill joined
  to a spike residual after a long unsure stretch was more than L from the step's first rise. Steps are disjoint and
  in order, so the greedy matching stays maximum; this only adds matches.
- `LineDiff`: new messages in the first frame after 3+ empty frames are late if any message was first read under
  `young_s` ago (it can't have faded, so the frame may show it misread). A faded copy of the same message in a slot no
  longer hides a different message seen there within `keep` frames (the new one is late, that one doubted). A slot
  read A, then B (new), then A again doubts B.

**Not done: M over trusted lines only.** The bound needs only trusted lines in M, so it holds under the three
assumptions; but suspect lines in M absorb residual step splits (one kill, two steps, its line read twice). `susp`
overcounted on 3 of 1,000 seeds with it (0 without), and the replay gained nothing (17 both ways). Left out.

**Probe (r6, 5,000 seeds, MIS off/on), over-count seeds:** base 0/0, mis1 0/0, gapread 0/1, barlow 0/2, barspike 0/0,
susp 0/0, suspfast 0/0 (`r6_probe.py`; its extra draw per kill changes the r4 generators' random streams).
`r3_probe.py` (6 generators) and `probe31`: 0/0. No party sim row passes real kills except the assumption-1 quest row
(208/209 for 200). **Not yet 0:** barlow MIS=1 seed 499 is one kill whose line and bonus line are both read again as
new, twice (assumption 3's uncovered case); barlow 2588 and gapread 3049 are not traced yet. **Recall:** the replay
counts 17 of 53 (45 if late and doubted lines counted); the young-message rule costs 4 of them.

**Changed (2026-11-25, r8, last round): one merge rule, one demote rule; three residuals accepted.** From two
new full-pipeline generators (`tests/party_probe.py`: `barlow2`, the bar read a few EXP low for 2-3 readings and
then true; `loot`, mesos and item lines among the kill lines).

- **barlow2 merge (`BarKills._fixes`).** One kill made two steps: the true reading after a low one held for two or
  more readings is a second rise, and the one-reading join no longer applies (213 of 1,000 seeds over at MIS=1, and
  over with misreads off too). A rise now joins the step before it, at the gate or after it left the gate while
  still undecided, if it is under half that step's EXP (`fix_frac` 0.5), comes within `fix_t` (3.5 s) of the step's
  first rise, at most one kill line was read from `fix_s` (0.3 s) before the step until now (its own, maybe read
  late), and the step was never corrected before (even if that correction was taken back). If the bar later takes EXP
  back, the joined EXP is taken first, before the step is dropped. The bar's high mark comes down with it only if the
  joined EXP was seen in under two readings (a one-reading spike, as at the gate); seen in two, it stays, as when a
  step is dropped, or the same misread read again later would be new gain and a step of its own (re-check of
  cdc9a10: 3 for 2 kills, `test_a_misread_taken_back_from_a_corrected_step_makes_no_step_when_read_again`). Keeping
  the high mark on every take-back also passes, but costs barspike recall (0.63 → 0.54 at MIS=0). Tuning: at 0.25, 6 of 1,000 barlow2 seeds still
  overcounted with misreads off (a low read of up to 60 EXP is over a quarter of a 189 EXP kill); without the
  one-correction limit, kills whose lines aren't new (an area full of one line) all joined one step and `base` recall
  fell from 0.71 to 0.59; with "no line read after the step began" in place of "at most one line", a kill line read
  0.6 s after its rise blocked the join. This only merges steps.
- **Loot demote (`LineDiff`).** One misread row at a new position became a trusted line (loot seed 445): a one-row
  frame where nothing lines up, right after a frame that showed messages (no gap of 3+ empty frames), now returns
  its new message late. Applied to frames of any size it cost the replay 3 kills (17 → 14): there a new kill line and
  its bonus often replace a faded line in one frame. This only demotes lines.

**Probe (r8, MIS off/on), over-count seeds:** see the PR body for the full table. r6 generators at 5,000 seeds:
base 0/0, mis1 0/0, gapread 0/1, barlow 0/2, barspike 0/0, susp 0/0, suspfast 0/0. x generators at 2,000 seeds:
barlow2 0/0 (was 213 in 1,000 at MIS=1), loot 0/0 (was 1). `dup` (the whole area drawn one row off for a frame, a
scroll misestimate) isn't a real event and doesn't block: 146/101 of 2,000 (56 of 1,000 in r7). `r3_probe.py`
(6 generators) and `probe31`: 0/0 at 5,000 seeds. Sims: as r6 (only the assumption-1 row passes real kills,
208/209 for 200). Mean total/real moved by at most 0.04 against r6. The replay counts 17 of 53, as before.

**Accepted residuals (owner approved, 2026-11-25).** Each is +1 at most, transient (the run ends under real kills),
about 1 in 12,000 harsh-misread runs, and pinned in `tests/test_party_probe_seeds.py` (≤ real + 1):

- barlow MIS=1 seeds 499 and 2588: one kill read twice as kill line + bonus line, the pair identical each time, in
  frames with heavy digit misreads (assumption 3's uncovered case).
- gapread MIS=1 seed 3049: a row dropped just before a burst of failed reads, then its bonus line misread at 6 s to
  fade after the kill, so it reads as a new pair.

**Follow-up.** Calibrate the probe's misread rates (per-row digit drops, row misses, bar lows and spikes) against
real captures before adding any more late rules: each rule so far costs recall, and the probe rates are guesses.

**Accepted costs.**

- Kills that share one bar step count once unless their lines were read: AoE and multi-kill frames lose recall.
- A suspect line counts nothing. On a one-mob map with bonus lines missed, many real kills are suspect.
- Kills settle about L (6 s) after their line.
- A late line counts nothing when its step is matched to it: 4–5% of kills on the party 5-12 s sim (191 of 200
  against 200 without the late rule), and the replay drops from 47 to 42 of 53 (L = 6 s alone: 45; the late rule:
  2 lines after a 3-frame gap that may be re-reads, 1 real kill read in a rejoin frame).
- A quest's EXP line during a grind is a trusted line (assumption 1): the sim row "party quest w/ EXP line 60 /10"
  counts 219/217 for 200 kills, as on main.

## 2026-11-25 — Overlay: one fixed size, EXP so far in the first minute

- **Fixed size.** At a given `overlay_scale` the overlay is the same size in every state (idle, waiting, not a
  hunting map, first minute, active, paused, no loot or kills yet). Every slot has a set width and height; hints swap
  text in place, empty rows keep their space with a quiet placeholder, and long values elide inside their slot (a big
  figure steps its font down instead). Only the resize grip or the scale setting changes the size.
- **First minute.** The big figure is the EXP earned so far ("+1,240 EXP", hint "first minute"), not a blank. EXP/min
  takes over at 60 s. ETA and vs-average stay as dashes until then. The main window's live headline follows the same rule.
- **Shared tile.** A boxed sprite with its count drawn over the bottom-right corner (outlined; red for items used) is
  the one tile for the overlay's rows and the main window's loot grid (`overlay.Tile(badge=True)`).
- **Kills slot.** `overlay.kills_summary` is the one place the overlay builds its kills headline (the total for now);
  the self / party split from #30 only has to change what it returns.
- **Text floor.** No overlay text under 11 px at 100% (it scales with the overlay below that).
