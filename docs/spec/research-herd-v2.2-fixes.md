# v2.2 Fixes Review (revision list for Research Herd v2.2)

```
Revision list for Fable: Research Herd v2.2
Conclusion: The direction is right. Keeping v2's task shape and not assigning work by
leverage is correct. The problem is that D72 to D78 were added as standalone paragraphs
and were not wired into the schema, the component table, the arm switch table, the weekly
plan, the L0 tests or Appendix C. An implementer who follows §5, §6, §14 and Appendix C
will get a system with no Integrator; one who follows D74 gets an Integrator that exists
only in S2.

Target: ~/Documents/Research-Herd-Design-v2.2.md (1,130 lines). Line numbers refer to
this version.

```

*English translation of the Chinese original (`zh/research-herd-v2.2-fixes.md`). Its changes are already applied to v2.2 (round 30); it is kept as a record. Line numbers refer to the Chinese v2.2 file.*

**Usage:**
- Items marked **[Needs a human decision]** are design decisions. Ask a human first; don't decide them yourself.
- Everything else is a text change. Each item says where to change and how.

---

## 0. Four things to ask a human to decide first

1. **Whether the Integrator runs in every arm** (recommendation: yes; see section 1).
2. **D78 pre-screening**: defer it for the H1 phase, or make it the same in every arm and have it done by a pre-screener who does not read the eval code (recommendation: defer; see section 4).
3. **Whether S1 workers can receive `baseline_advanced` and the updated map** (recommendation: yes. It is a score-type event, and S1 already shares scores).
4. **Pushing back the weekly plan**: the pilot can only run after all S2 components are built, and week 3 cannot fit "build-out + pilot + preregistration + formal run" (recommendation: pilot at the end of week 3; in week 4, preregister and run the formal H1; shift the original week 4 content back one week).

---

## 1. [High] The Integrator exists only in S2, so the H1 difference mixes two things

**Problem**
- D74 (lines 468–477) gives S2 an Integrator that runs during the run: first in, first out; rebase; a paired remeasure if the baseline has moved; merge only above 1 standard error; send a compose task on conflict.
- The control arms still use a post-run `git apply --3way`, applied greedily by mean (the last item of D74).
- D62 (line 638), D65 (line 526) and the last row of D58 all say "all arms use the same mechanical merge process", and item 1 of D42 has the same intent. The text contradicts itself.

**Example** (cache hit rate problem)
- Workers A, C and D all change `prompt_builder.py`.
- B1+M applies greedily after the run: A goes in first; C and D conflict with A and are skipped.
- S2's Integrator rebases C and D onto A, sends a compose task for the conflict, remeasures, and merges.
- Of S2's extra score, you cannot tell how much comes from "sharing" and how much from "a better merger". Given the same Integrator, B1 might recover most of C and D on its own.

**Fix** (recommended; needs human confirmation)
- **The merger is the same in every arm.** The Integrator runs during the run in every arm, with the same configuration: acceptance rule, gates, guard batches, spot checks. Remeasures spend each arm's own top-level eval slots.
- **"Sharing" means only whether workers can see baseline changes.** That is, whether they receive `baseline_advanced` and rebase:
  - S2: on;
  - B1: off. Workers keep working on baseline@0. The Integrator merges their verified candidates in the background, and the workers cannot see it;
  - S1: see item 3 of section 0.
- **Who runs compose tasks.** In B1 they cannot go to B1's workers, or isolation breaks. So in every arm, compose tasks go to a fresh agent outside the worker pool: a new one each time, carrying no context from any arm, with its output going only to the Integrator. S2 workers can still take D64's "combination candidate" tasks; that counts as sharing.
- **Final deliverable.** For each arm: the last non-`guard_pending` baseline of that arm's Integrator. Keep the B1-single diagnostic column.
- **Matching text changes:**
  - delete the last item of D74 (control arms apply greedily with git apply after the run);
  - rewrite D62's description of merging; D65's "H1 tests the increment of in-run sharing over post-run merging" (change it to "the increment over no sharing under the same merger"); and the last row of D58;
  - in the H1 definition on line 112, change "best-of-N run in isolation and then mechanically merged after the run" to "best-of-N run in isolation and merged by the same Integrator";
  - update the D62, D65 and D74 rows of the register to match.

**New rows for D58** (recommended)

| Switch | B0 | B1 | S1 | S2 |
| --- | --- | --- | --- | --- |
| Integrator (D74) | On, same config | On, same config; invisible to workers | On, same config | On, same config |
| `baseline_advanced` pushed to workers, rule 6 | N/A | Off | **[Needs a human decision]** | On |
| Who runs compose tasks | A fresh agent outside the pool | Same | Same | Same; workers can also take combination tasks |
| Spot check (D72) | Same rule | Same | Same | Same |
| Egress allowlist (D75) | On | On | On | On |
| Restart that does not rely on checkpoints (D77) | On | On | On | On |
| Pre-review (D78) | Per the decision in section 4, the same in every arm | Same | Same | Same |
| `map/` (D76) | Only the initial map on baseline@0 | Same | **[Needs a human decision]** | Fully on, updated with `baseline_advanced` |
| Final deliverable | The Integrator's last non-`guard_pending` baseline; B1-single as a diagnostic | Same | Same | Same |

---

## 2. [High] D72's spot-check rule raises frequent alarms even when there is no overfitting

**Problem 1: the two sides of the comparison are asymmetric.** D72 (line 643) compares two things:
- on daily data, the sum of the gains measured when each merge was accepted;
- one measurement on spot-check data.

The measurement taken at acceptance is itself biased upward, because only what looks good clears 1 standard error (winner's curse). This bias accumulates linearly with the number of merges, while the threshold 2σ√k grows only as √k.

> **Example:** At σ = 0.6, each accepted measurement is biased upward by about 0.5 on average (simulated with EXP1's gain distribution). After 8 merges, the cumulative bias is about 4.0. Reading k as "counted from the start", the threshold is 2 × 0.6 × √8 = 3.4, so the rule raises an alarm even with no overfitting at all. The longer the run, the more certain the false alarm.

**Simulation (EXP5, no overfitting at all):**
- Reading k as "merges in this segment": 16–33% false alarms per spot check.
- Reading k as "cumulative from the start": 31–75% false alarms per spot check.
- At least one false alarm per run: 58–99%.

**Cost of each false alarm:**
- bisection spends 2 spot-check slots;
- a good merge is reverted;
- its claim is quarantined for human review.

**Problem 2:** k can be read in two ways, and the text does not say which.

**Problem 3:** B1+M, which merges only after the run, has no step-by-step measurements on daily data, so the rule cannot be computed. If section 1 is adopted, B1 also has an Integrator, and this problem goes away.

**Fix** (rewrite the second item of D72)

> Spot-check once every 2 merges. For the same segment (from the last spot-checked baseline@j to the current baseline@k), make two new paired measurements at the same time: one on daily data and one on spot-check data. Flag only if their difference exceeds 2.5·√(σ_daily² + σ_spot²) (σ is the single paired-measurement noise obtained from each dataset's A/A remeasure; when the two datasets are the same size, this is 2.5σ√2). The new measurement on daily data uses the spot check's separate capacity. It does not count against the hourly rate limit, and it does not count toward the 10-call cap on `holdout_run` (it does not touch spot-check data). Same in every arm.

- **False alarm rate of the new rule (EXP5):** with threshold 2σ√2, about 2.3% per spot check and 8–15% per run; with threshold 2.5σ√2, about 4% per run.
- **Register:** update the D72 row to match.

---

## 3. [High] D72 to D78 never made it into the implementer's path

Appendix C tells the implementer to write interfaces.md "treating §5 and §9 as authoritative", with the scope being "the before-the-gate column of §14". None of the places below were changed, so the implementer will build the old version.

### 3.1 §5 event schema (from line 294)

**`type` list:** add `baseline_advanced | baseline_reverted | integration_failed | interaction_found | map_updated | audit_checked | dedup_hit`. Right now they are mentioned only in one sentence in the body text of D76.

**`role` list:** add `integrator`. In D34's priority order, put integrator at the same level as Eval service / CI.

**Fields of the new events** (recommended)

```yaml
# baseline_advanced
  baseline: 7
  parent_baseline: 6
  candidate: <hash>
  files_changed: [prompt_builder.py]      # rule 6 compares this with lease.files_touched
  paired: {mean, se, n_items, n_reps}
  guard_pending: true | false
# baseline_reverted (records are append-only)
  from_baseline: 9
  new_baseline: 10
  removed: [<hash>]
  reason: audit | guard:<name>
# integration_failed
  candidate, baseline, paired, reason: below_1se | gate:<name> | conflict_unresolved
# interaction_found
  candidate, baseline, earlier: {mean, se}, remeasured: {mean, se}, overlaps_with: [<hash>]
# audit_checked
  segment: [j, k], daily: {mean, se}, audit: {mean, se}, threshold, flagged
# map_updated
  baseline, measurements: [<id>], output: map/measurements/<id>/<baseline>/
# dedup_hit
  candidate, original_claim
```

**The fields of `result_ready`** (lines 303–310) and **the text of D57** (line 453):
- Replace `score / n / ci` with: `baseline`, `paired: {mean, se, n_items, n_reps}`, `per_item:` (a pointer).
- For ratio metrics, also add a bootstrap interval.
- The absolute score can stay as a diagnostic field.
- `n_reps` counts only the remeasures that the Eval service runs itself (D73).

**The signature of `eval_request`** (line 357):
- Add a `baseline` parameter, defaulting to the current baseline; the return value also carries `baseline`.
- The Integrator's remeasures go through the internal interface `remeasure(candidate, baseline)`. They go through neither `needs_confirmation` nor dedup.

### 3.2 D69 candidate hash (line 268)

**Problem:** The text still says "hash = content hash of the payload". D73 says to change this, but D69 itself was not changed.

**Example:** E's truncation change touches only `context.py`. After A is merged (it touches only `prompt_builder.py`), the diff that E produces after rebasing is byte-for-byte identical. Under D69's hash, the remeasure hits `dedup_hit` and returns the old score. So the Integrator's remeasure silently does not run, and that remeasure should have caught "E's truncation interacts with A's reordering". L0 item 4 tests exactly this dedup under the old semantics.

**Fix:**
- hash = hash(baseline identifier + normalized payload).
- The manifest's `target` must be a baseline identifier. A target-repo sha is no longer allowed.
- Similarity search (`needs_confirmation`) still uses the payload only.
- Update the D69 row of the register to match.

### 3.3 D32 lease (line 228)

**Problem:** Rule 6 (line 402) uses `files_touched`, but the lease has no such field, and nothing says who maintains it.

**Fix:** Add `files_touched` to the lease. The Executor sidecar writes it at each heartbeat by running `git diff --name-only` on the sandbox (it already sends a heartbeat every 30 seconds).

### 3.4 D9 state machine (from line 252)

**Problem:** D72 says the offending claim is "quarantined for human review", and D76 says to add `quarantined`, but the state machine table has no such state.

**Fix:** Add `verified → quarantined → verified | refuted` to the result row. The Integrator sets a claim to `quarantined`; only a human can release it.

### 3.5 D55 tasks (line 280)

- Add `integrator` to `created_by`.
- Add two goal templates:
  - "Rebase candidate c-x onto baseline@k and resolve conflicts" (compose);
  - "Follow-up after integration\_failed".

### 3.6 §4.1 L2 list and §6 component table (from line 315)

Change "ten components, five without a model" to "eleven components, six without a model", and add a row:

| Component | Model | Responsibility | Trigger | On failure | Source |
| --- | --- | --- | --- | --- | --- |
| Integrator | None | The only writer of the baseline branch. Takes verified candidates first in, first out: rebase; if there is a conflict, send a compose task; if the baseline has moved, request one paired remeasure; merge according to the acceptance rule. Emits `baseline_advanced` and other events. Every 3 merges, runs the expensive gates as a batch; if a problem is found, bisects and writes a new baseline that excludes the culprit. Triggers a spot check every 2 merges. After `baseline_advanced`, reruns the measurement scripts in `map/`. After the run ends, carries out the closeout order (section 7) and attribution | A result claim reaches verified; the budget runs out | The queue is in KV; recover from `baseline/<k>.json` | D74 |

Also state how the two branches relate (recommended):
- the merge queue writes main of the blackboard repo (claims, events);
- the Integrator writes the baseline branch of the repo of the system being optimized.

Right now D56 says "the merge queue is the only process that writes main", and D74 says "only the Integrator can write the baseline branch". How the two relate is not explained.

### 3.7 §14 before-the-gate checklist and weekly plan (from line 756)

**Add to the before-the-gate checklist:**
- D73 paired measurement;
- D74 Integrator;
- D72 spot checks and the pilot;
- D75 egress allowlist;
- D77 restart path (D77 itself says it is "usable before the gate");
- D76's map rerun, plus the first batch of measurement scripts written by hand by a human (section 8).

**Weekly plan:**
- **Week 2:** add the Integrator and paired measurement. Change the original "B1 @ 5 trial run to measure variance" (one arm, one run) to D72's pilot: 3 batches each of B1+M and S2, with the arms in the same batch running in parallel, plus an A/A remeasure for each source.
- **Timing of the pilot:** the pilot requires S2 to be fully on, so it can only go after the Router, hooks and Librarian are built. Push back the weekly plan as in item 4 of section 0.

### 3.8 §14.1 L0 tests (from line 815)

- **Item 4:** use the new hash.
- **Item 11:** if section 1 is adopted, the post-run greedy merge no longer exists. Change this item to test the Integrator (see item 14 below).
- **Add items 13 to 20:**

13. Changing only the baseline identifier changes the candidate hash. `result_ready` carries the baseline, paired gain, standard error and n (D73).
14. Integrator: text conflict → send a compose task; no conflict but the baseline has moved → exactly one paired remeasure; gain does not clear 1 standard error → `integration_failed`, plus a follow-up task for the author (D74).
15. The `files_changed` of `baseline_advanced` intersects `files_touched` → hard interrupt; no intersection → soft interrupt (rule 6).
16. A guard batch fails → bisect and write a new baseline that excludes the culprit, leaving old records unchanged; the deliverable is never `guard_pending`.
17. Spot check: a scripted overfitting candidate is flagged, reverted and quarantined; a clean scripted run raises no alarm in any of its 7 spot checks (this can pass only with the new rule from section 2).
18. A worker sandbox accesses a host outside the allowlist → refused (D75).
19. Context usage above 70% → write a claim and open a new session with the start pack; `kill -9` → the sidecar writes a stub claim on its behalf (D77).
20. Arm fairness: the Integrator configuration is the same in every arm; B1 workers do not receive `baseline_advanced`.

### 3.9 Appendix C (from line 1071)

- **The interfaces.md checklist:** add the format of `baseline/<k>.json` and the fields of the new events.
- **Item 2 of "Day one after the plan is approved":** change "walk through the D62 merge" to "walk through the Integrator and the `holdout_verdict` flow".
- **Definition of done:**
  - change "the 12 L0 items" to "the 20 L0 items";
  - change "S2 vs B1+M @ 5 × 3 runs" to "the number of runs computed from the D72 pilot; if the budget is not enough, label them as exploratory runs" (section 5).

---

## 4. [High] D78 pre-screening leaks eval knowledge, and the arms are not equal

**Problem**
- **It leaks eval knowledge.** Veto reasons are written as public claims, but the Critic doing the pre-screening is "the only agent that can read the eval code" (line 327). D11 keeps reasons private precisely because "why it scored" is a hacking manual; D63 says the same thing.
  > **Example:** A worker adds a fixed 2,000-token prefix. The hit rate (cached tokens / total tokens) goes up, but there is no real improvement at all. Once the Critic's veto reason, "the score is the share of cached tokens, and fixed padding inflates it", is public, every S2 worker can find it by search.
- **Nobody runs it in the H1 phase.** §14 puts the Critic's LLM audit after the gate (line 771), and H1 runs before the gate.
- **The arms are not equal.** D58 has no row for pre-screening. If it is on only in S2, S2 wastes fewer eval slots. That has nothing to do with sharing; it is yet another confound.
- **The rate limit is unclear.** The text does not say whether the Critic's 6 per hour (line 327, D48) includes pre-screenings, and each candidate needs review by 2 Critics.
- **Compose contradicts itself.** After compose is dispatched in parallel to several agents, it "takes the version with the best paired score". Every version costs a top-level eval, which contradicts "spends no eval".

**Fix (needs a human decision; recommend a)**
- **a.** Defer D78 for the H1 phase. Mark it in the register as "Deferred; separate A/B after H1".
- **b.** Keep D78, but:
  - create a new "pre-screener" role that cannot read the eval code and reads only the candidate, the spec and the public conclusions of the audit;
  - the same in every arm;
  - in B1, veto reasons go only to the author; in S2 they are written as claims (this counts as sharing);
  - it does not count against the Critic's rate limit.

Whether you choose a or b: pick among compose's multiple versions using the fast tier only. Only the chosen version goes on to the slow tier.

---

## 5. [Medium] The number of runs is hard-coded as 3, which contradicts D72's statistical power calculation

**Problem:** The following places hard-code "3 seeds" or "3 runs":
- H1 on line 112;
- the §11 table on line 675 (column 2);
- week 3 in §14;
- the scope and the definition of done in Appendix C.

But item 4 of D42 says "first use 1 trial run to measure variance", and D72 says the pilot runs 3 batches and the number of runs is computed from statistical power. The three places contradict one another.

**Simulation (EXP7):** one-sided paired t-test, α = 0.05. The effect is in units of "the standard deviation of the batch differences".

| Runs | Effect 1.0 | 1.5 | 2.0 | 2.5 |
| --- | --- | --- | --- | --- |
| 3 | 32% | 53% | 71% | 85% |
| 5 | 58% | 87% | 98% | ≈100% |
| 6 | 67% | 93% | 99% | 100% |

With only 3 runs, the effect has to reach about 2.3 times the run-to-run noise to get 80% power. If the effect is 1.5 times the noise, 3 runs have only an even chance of detecting it, and "not significant" says almost nothing.

**Fix**
- In each place listed above, change "3 seeds / 3 runs" to "computed from the pilot per D72".
- Keep "at least 3 per arm" in item 3 of D42 as a floor.
- Change "1 trial run" in item 4 of D42 to "the D72 pilot (3 batches)".
- Run the arms of the same batch in parallel. One run of testbed problem A takes 48 hours; running them serially will not finish.
- Optional: include B0 in the pilot as well. It is cheap, and it gives an early look at "whether the problem should be split at all".
- **The pilot uses spot-check data:** the pilot has to evaluate each deliverable 5 times on spot-check data, which exceeds D66's cap of 10 per arm per run. Choose one:
  - count it toward that pilot run's own cap of 10, with correspondingly fewer in-run spot checks;
  - cut a separate dataset for the pilot.

---

## 6. [Medium] The 70% rule for `interaction_found` gives false alarms more than half the time

**Problem:** If the remeasured gain is less than 70% of the earlier gain, an `interaction_found` is recorded (D74). But the earlier measurement is "the one that cleared 1 standard error", so it is itself biased upward.

**Simulation (EXP6, no interaction at all):** at σ = 0.3 / 0.6 / 1.0, the false alarm rates are 43% / 55% / 65%.

**Consequence:** A run has about 10 merges, so it produces about 5 interaction claims, most of them false. They go into `map/` and the digest, and S2 workers spend time explaining things that don't exist. What gets polluted is exactly the shared information that S2 relies on to win.

**Fix**
- Record it only if the earlier gain minus the remeasured gain > 2σ√2 (EXP6 false alarms 5–9%). Or, when the gap exceeds 70%, do one more paired remeasure and write it only after it is confirmed.
- State the relationship with D64: in D64, the Critic also writes insights with `tags=[interaction]` (line 522). Recommendation: the Integrator only emits the event, and the Critic writes the insight only after reviewing it.

---

## 7. [Medium] The end-of-run steps are not defined

**Problem**
- **The last few baselines are unchecked.** When the budget runs out, the last few baselines are most likely `guard_pending` (gates are checked in a batch every 3 merges). The text says "the deliverable is never `guard_pending`", but does not say what to do:
  - fall back to the last checked baseline?
  - or run the missing checks outside the budget? If so, whose capacity do they use, and is it the same in every arm?
- **When the final spot check happens.** Before or after the missing checks?
- **Data for attribution.** Attribution (removing approaches one at a time) is outside the budget and needs at least one eval per approach, but the text does not say which data it uses. Using `holdout_verdict` would break "touched only once in the whole experiment".
- **Switching to the strict version.** D74 says "switch to the strict version when more than 40% of the budget is expected to go unused". This switches rules during the run:
  - the pilot measured noise under the default rule, so after a switch the preregistered power calculation no longer holds;
  - if one arm switches and another does not, the arms differ again.

**Fix**
- **Hard-code the closeout order, the same in every arm:**
  1. stop accepting;
  2. run the missing gate checks on all `guard_pending` baselines (a fixed quota outside the budget, the same in every arm; if a problem is found, bisect following the existing process);
  3. final spot check;
  4. freeze the deliverable (the last non-`guard_pending` baseline);
  5. after all runs have ended, evaluate `holdout_verdict` for all of them together.
- **Attribution:** do it after `holdout_verdict` has been evaluated, using daily data only, with a fixed quota, so it does not affect the verdict.
- **During the H1 experiment, no rule switching during a run:** choose the acceptance rule at preregistration and write it into the preregistration. If you want the strict version, use the strict version in the pilot too.

---

## 8. [Medium] Nobody owns the map

**Problem**
- D76 says that after each `baseline_advanced`, pure code reruns the measurement scripts in `map/`. But nobody is responsible for writing the first batch of scripts:
  - they are not among the charter's four hand-written files;
  - they are not any component's responsibility;
  - they are not in the weekly plan.
- D58 has no row for the map either.

> **Example:** In the cache hit rate problem, the most useful shared measurement is a script that breaks down cache misses by cause (timestamps, tool order, document position, truncation). As the text stands now, someone will build a "rerunner" with nothing to run.

**Fix**
- A human writes the first batch of measurement scripts by hand when building the problem. List them in the §14 before-the-gate checklist, alongside spec.md and the first approaches. Write the first approaches against `map/gap.md`.
- The Integrator does the rerun after `baseline_advanced` (section 3.6).
- S2 workers can add new scripts to `map/measurements/`, going through `repo_write` and CI's schema check.
- Add a row to D58 (already listed in the table in section 1).

---

## 9. [Low] Wording fixes

- **D48 (line 891):** "Librarian every 10 commits" contradicts P10 and §6's "runs continuously, not in batches". Delete it.
- **D14 register row (line 857):** the on-failure action "switch to a stronger model" no longer means anything under P10 (every role uses the strongest model). Change it to "check the digest structure, or add a Librarian per approach".
- **D74's "doubles the net gain compared with 'remeasure three times and look at the 95% lower bound'":** correct it according to the simulation (EXP1):
  - against "remeasure three times and look at the 95% lower bound", it is about 2.9 times (30.7 / 26.7 / 22.8 vs 10.5 / 9.2 / 8.0, at σ = 0.3 / 0.6 / 1.0);
  - about 2 times is against the strict version (14.9 / 13.3 / 11.7);
  - under EXP1b's more realistic setting, it is 1.2–1.6 times.
- **D74's "near-zero harmless patches":** in the simulation these merges are slightly harmful, −0.3 to −1.4 per run. Change it to "slightly harmful patches".
- **D74's "more than 1 standard error":** state that this is the standard error of a single paired measurement. If the baseline has moved, use the remeasure's; if it has not, use the verification's.
- **D72's "σ\_run taken as 1.5 times (only a 70% upper bound)":** with 3 batches (2 degrees of freedom), 1.5 times is about a 64% upper bound; with a pooled estimate from both arms (4 degrees of freedom), it is about 78%.
- **D73's "the paired difference between any two candidates can be computed afterwards":** this holds only if the eval is deterministic under the same seed, as with problem A's simulator. Problem B involves model calls, so they still have to run in the same batch and in the same time window.
- **D75's "declared context sources":** there is no place to declare them. Add a sources list to `charter/`, or put it in `eval/sources/`.
- **_Research Herd v3 Review_ (not in the repo):** line 18 and the last item of D72 cite it. The file is already in the same directory (`Research-Herd-v3-Review.md`). Write the filename where it is cited, so implementers can find it.

---

## Appendix: Simulations

- **Script:** `~/Documents/herd-sim/v22_checks.py`. The output is in `v22_checks_out.txt` in the same directory. It takes about 3 seconds to run.
- **Setup:** reuses the setup of EXP1 in `herd-sim/` (simulation scripts, not in the repo). The gain distribution is 60% N(0, 0.3), 25% N(−1, 0.5), and 15% exponential with mean 1.5. The acceptance rule is a single measurement above σ.

**EXP5: spot-check false alarms (no overfitting at all)**

| σ | Budget | Mean merges | Per-segment reading: per check / per run | From-start reading: per check / per run | New measurement, 2σ√2: per check / per run |
| --- | --- | --- | --- | --- | --- |
| 0.3 | 40 | 10.8 | 16% / 58% | 31% / 60% | 2.2% / 11% |
| 0.3 | 96 | 25.9 | 16% / 70% | 36% / 73% | 2.3% / 15% |
| 0.6 | 40 | 8.8 | 25% / 67% | 49% / 75% | 2.3% / 9% |
| 0.6 | 96 | 21.1 | 25% / 86% | 61% / 94% | 2.3% / 15% |
| 1.0 | 40 | 7.8 | 33% / 74% | 60% / 83% | 2.4% / 8% |
| 1.0 | 96 | 18.8 | 33% / 94% | 75% / 99% | 2.3% / 15% |

**EXP6: `interaction_found` false alarms (no interaction at all)**

| σ | 70% rule | Difference > 2σ√2 instead |
| --- | --- | --- |
| 0.3 | 43% | 5.0% |
| 0.6 | 55% | 6.8% |
| 1.0 | 65% | 8.6% |

**EXP7: power table** — see section 5.
