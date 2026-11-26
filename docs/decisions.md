# Decisions

Architecture decisions, newest first. A decision that changes an older one says so; the older one gets a dated
"Changed:" line.

## 2026-11-26 — Session time is the wall clock; POTIONS is mesos

- **Clock.** Session time runs from the first EXP gain and stops only while paused (the pause button). It no longer
  stops while idle. AVG/min, EXP/hour, the ETA, net mesos/hour, the potions per-minute figure and the per-minute history
  (idle minutes show as 0) all divide by it, so they match maplestory.watch. Owner decision, 2026-11-26.
- **Idle** is a status badge only. The current rate (the headline EXP/min) is the net EXP of the last 60 s of wall
  time (`CURRENT_RATE_S`), so it falls smoothly to 0 within a minute of the last gain, as on watch. It goes negative
  after a death. The EXP tracker's own 600 s window is unchanged.
- **Two clocks.** `Session.active_s` is the wall clock (saved as `active_seconds`; the key is unchanged but the value is
  now wall time minus paused time, so older files read shorter). `Session.grind_s` is the old idle-skipping clock; only
  the EXP tracker's readings use it (its step caps and spike windows are tuned to it). Kill/EXP matching runs on
  the monotonic `now`, which is separate and unchanged.
- **POTIONS** shows the mesos spent on items used (`Session.mesos_spent`, at the user's buy prices) with a -N/min line,
  not the item count. Items with no price are left out and the figure gets a "+" and a tooltip with the count. No
  price is ever guessed. NET MESOS is still mesos picked up minus that cost.

## 2026-11-25 — Self vs party kills from the EXP line's colour

Issue #30. Builds on "Party kills: event matching, not magnitude pricing" below; its count is unchanged.

**Context.** In a party, "You received EXP" is white for your own kill and yellow for a party share. The party
recording shows this, and the bonus line under either is yellow. Sampled from the recording, white reads
~(250,248,244) and yellow ~(238,246,100); video chroma pulls a game yellow's blue up. Both dim as they fade, and
over the tree trunk they tint orange.

**Decision.**

- **Colour.** `msg_row_colour` (reader) classifies a message row from its brightest core pixels by shape, not by
  distance:
  - white needs blue/max ≥ 0.8; yellow needs blue/max ≤ 0.6;
  - both need |red − green|/max ≤ 0.1;
  - the winner needs 70% of the core pixels, and the other class at most 10%.
  - Otherwise the result is None (unknown). Faded rows (brightest under 160), scenery and mixed rows are unknown.
- **Carrying it.** `LineDiff` gives a new message the colour of its row in the frame where it is new (`Message.colour`,
  not part of equality). `BarKills.line` keeps the colour, and only uses it to sort a counted kill.
- **Split.** `BarKills.by` counts every kill once, by where it came from:
  - `solo`: the solo rule;
  - `white`, `yellow`, `unknown`: a trusted line counted in a party, by its colour;
  - `step`: a step that no line matched.
  - Self = `solo + white`. Party = `yellow`. Unknown = `unknown + step`. These sum to the total.
- **A repeated value is no colour's kill.** Suppose a counted line repeats the value of a line read within
  `colour_repeat_s` (12 s) before it, and the two colours could match (the same colour, or either unknown). Then it
  counts in the total but goes to unknown. The total's bound leans on assumption 3 (a trusted line is never a
  re-read), and the colour probe breaks it: a row fails to read, then shows again as new with its bonus line, 0.4 to
  9.0 s after the first read. Examples are `rowfail` seeds 255, 985, 990 and 1165, `gapread` 966, `flip` 576,
  `barspike` 637 and `barlow` 1088. That made 8 of 47,500 runs put self or party over the truth while the total
  stayed under, because other kills were missing from it. Since the total has slack and each colour has less, a
  colour count needs a line that cannot be a re-read. 12 s covers the longest gap seen (9.0 s), with margin.
- **A step is never a party kill.** A step with no line may be one of your own kills whose white line was missed,
  so it stays unknown. The issue's plan ("party = total − self") would count such a kill as party.
- **Per mob.** A white line is your own kill: `KillCounter` keeps it off the share path even after its bonus line. It
  is classified by the solo fit, with both guards kept: its value seen twice (`_solo_ok`), and a trusted fit with 2+
  clusters (`_solo_trusted`). A yellow line is a share even with its bonus line missed. A share or unknown line goes
  only through the top-mob window. The monsters table shows KILLED as "self · party", plus "(+N?)" for lines of
  unknown colour. Solo play is unchanged.

**Invariant.** The total is unchanged (`trusted lines + steps − M`), so it stays ≤ real kills under that entry's
assumptions. Each counted line is a trusted line, so it is a distinct real kill. With assumption 4 below, a white
counted line is a distinct real own kill: self ≤ real own kills. A colour count needs no
assumption 3: it is only given to a line with no value-and-colour twin read within 12 s, and a re-read shows within
that time of its first read. Likewise party ≤ real party kills. Steps and lines of
unknown colour go to neither. A white line that may be a re-read is suspect, so it is never counted, and never as self.
A counted white line is matched to its step (it is in M), so its kill is not a step too.

4. **A row read white or yellow has that colour.** The classifier abstains rather than guess: the two classes are far
   apart on blue/max (~1 vs ≤ 0.5), and a fading white over the trunk fails the red ≈ green test, not the blue one
   (`test_faded_white_over_the_trunk_is_never_yellow`). The probe models colour errors as unknown reads only.

**Probe.** `tests/party_probe.py` takes `colours=True`. It draws each kill's colour (0%, 30%, 50% or 80% own) from a
separate random stream, so the seeds' kills and reads are unchanged. At MIS=1 it reads a row's colour as unknown at
2%, 10% or 30%. The PR body has the 5,000-seed table.

**Recall (replay, 53 kills: about 18 white, 35 yellow).** The total is 17, as before. It now splits into 2 self, 2
party and 13 unknown. Without the repeated-value rule it would be 10 self and 7 party, but the probe overcounts
then. Kills of one value come every few seconds here (+13, +17, +25), so most counted lines have a twin within
12 s. A window of 3 s would keep 6 self and 4 party but miss re-reads 5 to 9 s apart. Per mob is 0 before and after: the white values (24, 25, 41) fit no single scale, so the fit's
guards classify none of them. Per-mob self counts need white values at full mob value, as a lone kill gives.

**Accepted costs.** A kill counted from a step (no line read) is neither self nor party. On a one-mob map most
kills repeat a value within 12 s, so the split is mostly unknown there. Telling a re-read from a new kill of the
same value (e.g. from the bar step it brings) is the recall follow-up. A faded row is unknown, so
a kill line read only after it faded counts toward neither.

**Changed: 2026-11-26 — party kills are an estimate, with the proven count as its floor (owner decision,
2026-11-25).** With strict proof the party total stayed low (17 of 53 on the replay, about 10% in a live party of
6), and white lines carry only your damage share, so their value can't name the mob. Party kills now show a
watch-style estimate, "~N" (``BarKills.estimate``):

- every distinct kill line read in a party (not bonus lines; trusted, suspect and late lines alike), plus each bar
  step no line matched (a message area full of one line shows no new lines: on probe `base` seed 0 lines alone
  gave 3 of 206 kills), plus the solo rule's exact kills, never under the proven count;
- a re-read is not a kill (the careful part). Each new message carries its slot in rows (``Message.slot``). A line
  is a re-read when a line of the same value, or one digit off (a dropped digit), was counted under 12 s before
  within half a row of its slot, or within 1.5 rows if ``LineDiff`` placed it late or doubted it (a lost scroll
  puts it a row off), or within 1.5 rows of the same value counted under 1 s before. A late or doubted line one
  digit off any value read in the last 12 s is a misread wherever it shows (limiting it to 3 rows let misreads
  through: a late line's slot is often far off; 226 vs 17 of 16,800 probe runs over 5%). A re-read moves its counted line to
  its own slot, so the next re-read is measured from there (a glitch frame makes ``LineDiff`` count a scroll too
  many, so a line re-read twice lands two rows off). Lines are decided 3 s after their read, as ``LineDiff`` may
  doubt them later;
- a step booked with no line, then a line no step matched read up to 4.5 s after the step's window closed (10.5-15 s
  after its last rise), is one kill: the line's (a new line can show 6-10 s after its kill). In the proven count a
  trusted such line takes the step's place (same total, its colour), so the floor never books step and line for one
  kill; at the session's end a line still waiting claims such a step for the estimate. The window is short so a
  line two merged kills left unmatched can't take an unrelated step. ``match_s`` (L) is now 10.5 s, up from 6: a
  new line can show 6-10 s after its kill, and at 6 s such lines matched the next kill's step down a chain, so the
  floor went over the truth (probe ``latenew``: 11 of 400 runs; 0 at 10.5, for about 1 point of floor recall).
  Changed: 2026-11-26;
- own is solo + white lines, shared is yellow lines; a line of unknown colour and a step count in the total only;
- per mob, the estimate is spread by this session's identified kills (solo or white identifications, the top-mob
  window), else by the map's spawn mix (spawn points per mob in `map_*.json`), with largest-remainder rounding so
  the mobs add up to the estimate, and shown with "~".

The proven count (``trusted lines + steps - M``) and its invariant are unchanged; it is the "at least N proven"
floor in the tooltip and footer. Solo counts stay exact. The estimate may run slightly high: see the PR #51 body
for the 1,250-seed table (mean, p95 and seeds over by more than 5%, MIS 0 and 1). A wider "same value anywhere
within 12 s for a late line" rule held the probe lower but dropped real kills on the replay (24 of 53), where most
lines are late and values repeat every few seconds; the slot rule keeps 42 of 53 there (15 own of about 18,
26 shared of about 35).

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

## 2026-11-25 — Main window live tab follows watch's layout

- **Cards.** Player header (level badge, EXP bar with "cur / need · %"), map card (street small, map name large,
  channel pill, controls), the EXP headline, AVG / BEST boxes, an ETA card ("LEVEL 46 IN 3h 41m at your average"), and
  on the right the monsters table and "Items this session". Still our cream theme, never watch's dark navy.
- **Items this session.** The overlay's boxed tile (`EntryGrid`), loot green and items used red and signed, most
  first, 18 at most with "+N more". NET MESOS and POTIONS sit beneath with a per-minute figure. Picked-up mesos aren't a
  tile here: NET MESOS says it.
- **First minute.** Same rule as the overlay: the headline is the EXP so far with the hint "first minute", also in the
  waiting state (never "— EXP/min, so far"). The ETA card, AVG and the per-minute figures show a dash until 60 s.
- **Not read, so not shown.** The game doesn't give us the character's name or job, so the header has the level only.
  The minimap thumbnail would need new capture plumbing and is left out.
- **Empty states.** "No kills yet" (caption on the monsters header) and "Nothing picked up yet" (in place of the grid).
- Changed 2026-11-26: POTIONS is mesos spent, not an item count, and every per-minute figure uses wall time (see
  "Session time is the wall clock").

## 2026-11-26 — Overlay header: the map name owns the top row; map changes confirm faster

- **Header.** Row one is the map name (the rest of the width, 77% of the frame at 100%) and the pause / reset / hide
  buttons at its right. The channel chip and the session clock moved to a slim line under it (channel left, clock
  right). A long name was squeezed to "Forgotten E… III" beside the chip and clock. Amends the 2026-11-25 fixed-size
  entry: still one size in every state (both lines keep their slot when empty), now one line taller than before (+16 px at 100%).
- **Numerals.** The recognizer splits a numeral's thin strokes with spaces ("Entrance I II", "Entrance I I I"); these read
  as I or II, so III showed as I. `split_numeral` now joins a lone stroke in front of the numeral word ("I II", "I I I") and
  a glued tail in front of one, while the whole is still a Roman numeral. "II I" stays II: a longer fragment before
  the last word is as likely noise as a stroke. A read is never given strokes it doesn't have.
- **Map changes.** Two ways to switch sooner than the vote: a loading transition (unchanged), and now two exact reads in
  a row (`gamedata.exact_read`: no fuzzy match, no lookalike letters, no repaired numeral, not clipped) of another
  map that span at least `MAP_FAST_SPAN_S` (0.8 s: two back-to-back reads of one frame can't flip it), about 0.8 s. A numeral that is a prefix of the shown map's ("VIII" read as "VII": a window over the title's
  end) is never fast. Fuzzy reads still need 3 of the last 5 and a `MAP_CONFIRM_S` hold, now 2.0 s (was 3.0).
  The #49 stale-vote expiry is unchanged. A raw read of the shown place whose numeral the place lacks no longer
  counts as a vote for the shown map.

## 2026-11-26 — Live tab with no game or no session; the tracker attaches to the game by itself

- **Attach.** `CaptureWatcher` (capture.py) looks for the game window every 3 s (a timer, one window enumeration) while
  nothing is attached, and starts the capture when it appears, again after the game restarts. It no longer depends on
  "Start and stop with MapleStory" (the follower used to do this only when that setting was on) and no restart is needed.
  A start that fails although the window exists backs off 3 s, 6 s ... 60 s. The status line says "Waiting for MapleStory…".
- **Detach ends the session.** When the game window closes the session is saved like any other end and the last reading
  is forgotten: the clock is wall time, and after a restart the reads may be another character's. Each capture start
  has a generation; a reader loop or close callback from an older one stands down, so two loops never feed the tracker.
- **No game.** The map card reads "No game", the sub line "Waiting for MapleStory", and the player header is hidden.
- **No session.** The headline is "Not tracking" (or "Paused"), not the first minute's "0 EXP"; the ETA card is a dash with no
  "after the first minute" tail; the sub line says "Today: nothing tracked yet" when no session was saved today. The
  first-minute rule (2026-11-25, Overlay) is unchanged for a running session.
- **Header.** The level badge and EXP bar show only once a level or EXP has been read.
- **Money.** NET MESOS and POTIONS are a dash until the session has mesos, loot or used items.

## 2026-11-26 — Solo kills: the fewest exact sum

Owner rule: solo kill counts are exact; never overcount (undercount beats overcount); no EXP multipliers exist;
quest EXP never lands while grinding. The old solo rule (at most new lines + 1 kills per pool, the extra one only
for a value read in the last 30 s) quarantined fast grinding in a message area full of one identical line
(`LineDiff` sees nothing new there), multi-kill hits and bar-before-line kills.

- **Gate.** Bar gain is kill EXP when an EXP line was on screen (new or not, `BarKills.shown`) within `nearby_s`.
- **Count.** The pool counts as the fewest kills whose trusted values sum to it (within `tol`). Trusted values are
  every map monster's listed EXP plus read values as before. Fewest never passes the real kills while each kill's
  EXP is one of those values; extra values only lower it.
- **Spikes.** Kills beyond the new lines read wait until the bar confirms the gain (`unconfirmed`, or held for
  `stale_s`), and need no more kills than the new lines plus the most EXP rows on screen near the pool. A solo bar
  fall comes out of the pool, then out of what was quarantined since it last grew (`_solo_fall`), so a spike the
  bar takes back no longer hides the next real gain. A pool with a line no trusted value explains counts at most
  its lines.
- **Deaths.** A death booked right after a fall of one kill's EXP with that kill's line just read gives that kill
  back (the tracker drops the last kill before a death).
- **Known residuals.** A kill landing in the same bar reading as a death is invisible to the bar. In the solo probe
  some deaths still lose the kill before them (by one). Under heavy simulated bar misreads (5% of readings a digit
  misread, three monsters, multi-kill hits) the count can pass real kills by a few for a moment (main: by one).
