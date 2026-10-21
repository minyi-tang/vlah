# Herd Testbed: validating the herd's behavior on two small problems

A testbed for Research Herd: validate the herd's behavior on cheap problems first · Oct 18, 2026 · @Minyi

*English translation of the Chinese original (`zh/herd-testbed.md`). IDs, numbers and section numbers are unchanged.*

## 0. The one-page version

Imagine the worst case. The herd is built and plugged straight into a real project. It runs for two days, and at the end we find that the workers never used each other's findings at all. Everyone worked in isolation, and what came back was just the result of whichever single worker ran best. Those two days were wasted, and we can't tell which link broke.

The testbed exists to prevent this. It takes a cheap small problem that reproduces three features of the real problem: **there are many approaches to try, each attempt takes a long wait, and the final good result has to be combined from improvements in different approaches**. Then it lets the herd run on it for a few hours to two days, and tells us whether the herd behaves the way we designed it to.

What it is not: it is not part of the herd, not an anti-cheating security system, and not a performance benchmark. It is a guardrail of our own design.

Two problems. **A is a cluster scheduling policy in a simulator.** It has many approaches (queueing, admission, placement, preemption, per-site tuning). Each formal eval takes 10 minutes, the whole team gets only 2 per hour, we control the noise, and every eval is free. **B is a coding agent harness deliberately broken in five places.** It is a scaled-down version of what we ultimately want to use the herd for. Each eval costs real money and is noisy. A first, then B: A is cheap, so plumbing problems show up within a few hours; B is realistic, and we move to it once A passes.

What counts as passing: with the same formal eval quota and the same time, the herd must beat two control groups, "a single agent working alone" and "N isolated agents". The behavior metrics on the §6 scorecard must also meet target. A win with the wrong behavior counts as no win.

How to read this: §1–5 are for humans and explain why and what. §6–10 cover how to measure, how to build, and what to settle on for now. §11 is the spec for the implementing agent: repo structure, interfaces, fields, the concrete way to plant bugs, and how to accept the testbed itself are all there. The appendix explains that this was cut down from the previous version, "Ledger", and what was cut.

## 1. What kind of problem is worth testing the herd on

At first we thought "more material than one agent can read" was enough. It wasn't. An agent that can grep, sample and write scripts has no trouble with a few hundred thousand tokens of material, and that is exactly what a strong model does first. Test on such a problem and the single agent does very well, the herd can't win, and we would conclude that the herd is at fault.

Only one reason holds up: **the number of approaches that need iteration before you know whether they work, × the wait per round, exceeds the available wall-clock time.** One agent can only try approaches serially, one at a time. With 10 approaches, 5 rounds each, and 10 minutes of waiting per round, plus thinking and writing code, 48 hours is nowhere near enough. The herd can work in parallel.

But parallelism alone is not enough. N agents that don't communicate can also run in parallel, and that is exactly one of the control groups. The herd can beat it only through three things: improvements found in different approaches **can be combined into one deliverable**; a dead end one worker hit **is not hit again by others**; and the team's shared eval quota **tilts toward approaches that show promise**. So the problem must also satisfy these: the improvements really are composable (some combinations add up, some conflict); there really are traps (things that look good but are actually bad); and there really is quota pressure.

Two more conditions. The eval must be something we can simulate as "expensive, slow, scarce". And we must know the answer, at least part of it. Otherwise we can't judge whether the herd's behavior is right or wrong; we can only see whether the score is high or low.

By these criteria, all three earlier candidates were eliminated: 80 synthetic knobs, a codebase with planted slow spots, and multi-service incident attribution. Their difficulty was always "lots of material", which one agent with tools can handle.

## 2. The testbed's five parts

&#91;embedded content: Testbed · five parts and three arms\]

Look at the figure first. Workers run the fast tier locally over and over; formal scores come only from the slow tier on the right. Everything the three boxes do lands in the recorder below. The scorecard is computed only from the recorder, and the three arms are compared side by side.

**The thing being optimized.** A directory. What the agent receives is this directory plus a PI prompt. It clearly separates what may be changed from what may not, and includes the baseline version and the baseline score. For problem A it is the simulator plus a policy; for problem B it is the harness plus a task set.

**Fast-tier eval.** A command a worker runs on its own machine. Free, unlimited, uses public data, noisy results. Like an engineer running tests locally: cheap, but it doesn't count.

**Slow-tier eval.** An HTTP service. It is the "external eval" from the herd's point of view, and it looks the same as the platform team's benchmark will when we hook up a real project later. It queues submissions and releases them according to a quota shared by the whole team. It uses hidden data that workers can't see, runs multiple seeds or multiple passes, and returns results after 10 minutes, with confidence intervals and per-item breakdowns. It has only three interfaces:

```
submit(candidate, predicted_score, note) -> ticket     # submit; must include a predicted score
status(ticket) -> queued | running | {score, ci, per_item, constraint_ok}
history() -> quota used, past submissions and results (the isolated arm sees only its own)          # also runs / events / meta / final, see 11.3
```

**Recorder.** An append-only jsonl file, written automatically from three sources. The slow-tier service writes every submission and result. The herd's tool layer writes every blackboard read and write, fork, allocation and abandon. Workers self-report every fast-tier run. The scorecard is computed entirely from this file, not by someone reading through logs.

**Scorecard and control groups.** After a run, a script computes the §6 metrics from the recorder, with the three arms side by side.

Anti-cheating keeps only three rules, which is enough. Hidden data and scoring code live only on the slow-tier service's machine, not on worker machines. Every run uses a fresh repo and namespace, so nothing carries over from the previous run. Workers have no git push permission and can write to the blackboard only through the herd's tools.

**Interface with the herd.** The herd's Eval service is just a client of the slow-tier service, one or two hundred lines. Because the interface is the same as a real external eval, when we switch to a real project later the herd doesn't change a line; only the implementation behind the slow-tier service changes.

## 3. Problem A: a scheduling policy in a simulator

**What it is.** A cluster scheduling simulator written in SimPy, with public source. 6 data centers, each with a few dozen machines and a two-day job stream. What workers write is the scheduling policy: one Python file that implements four hooks, plus a parameter file.

```
order(queue, state)       -> queue order
admit(job, state)         -> whether to let it in now
place(job, free, state)   -> which machine to put it on
preempt(running, state)   -> whether to preempt anyone
params.yaml               -> the numbers used by the four hooks above
```

The policy runs in a subprocess. It can see only the queue and machine state at the current moment, not future jobs. Each decision step is limited to 50 ms. A step that times out is handled as the baseline would handle it; if more than 1% of steps time out, the whole run is void.

**Objective.** Lower mean job wait time is better. Hard constraint: no one of the 6 sites may have a p99 wait time worse than the baseline's. A candidate that violates this has its score voided, and its result is marked "violated".

**The 6 sites all differ, and that is the planted answer.** Each site's load characteristic matches one effective technique. There are 5 ingredients in total, belonging to different approaches:

| Site | Load characteristic | What works for it |
| --- | --- | --- |
| 1 | Heavy-tailed job durations; a few jobs account for most of the load | Backfill: let short jobs bypass long jobs queued ahead of them |
| 2 | Two peaks, morning and evening; idle the rest of the time | Admission smoothing: throttle at peaks so machines don't overload and slow down |
| 3 | Jobs have data affinity; on the wrong machine they run 3× slower | Locality-aware placement |
| 4 | Huge numbers of small jobs | Packing: put the same user's small jobs on the same machine to save startup overhead |
| 5, 6 | Ordinary, but jobs come with their own duration estimates | Order by estimated duration (a global ingredient) |

Two more things are planted. One **trap**: an extreme "shortest job first" can push mean wait very low, but site 1's long jobs starve and p99 blows up. The single-seed fast tier can't see this; the slow tier flags a violation the first time it runs it. One **conflict**: aggressive backfill and strict per-user fair-share quotas cancel each other out when both are on; either one alone beats both together.

We hand-build a reference policy with all 5 ingredients on and roughly tuned parameters. Its score is defined as "100% of the available improvement", and the herd's share of available gain captured is measured against it.

**Two eval tiers.** Fast tier: `tb fast <candidate_dir> --seed 1`, public trace, under a minute, local, unlimited. Slow tier: hidden traces, 20 seeds, 2 per hour for the whole team, results 10 minutes after submission. (The compute actually takes a minute or two; it deliberately waits until 10 minutes to simulate real queueing.) Results include the mean, the 95% interval, per-site p99, and whether the constraint was violated.

**Ingredient detectors.** They look at behavior, not code. Each ingredient has a small constructed scenario. For example, give the policy an obvious backfill opportunity and see whether it backfills. Running the 5 scenarios tells how many ingredients the final policy contains, no matter how the agent wrote it.

**Precheck before the herd.** On day 3, run the reference policy and the baseline on 20 seeds each. Requirements: the gap is at least 5× the noise standard deviation; each of the 5 ingredients turned on alone gives a visible gain; the trap really violates the constraint; the conflict really cancels out. If any one fails, change the simulator, not the herd.

**PI prompt.**

```
sim/ is the scheduler's simulation environment (source readable); policy/ is the policy currently in production: one Python file + one parameter file.
sites/ holds load profiles and public traces for 6 data centers, each with different characteristics.

Goal: lower mean job wait time is better, but no site's p99 may get worse than it is now.
Locally you can run single-seed versions on the public traces as much as you like (under a minute, noisy). The formal eval is the platform team's service; it uses traces you can't see,
runs 20 seeds, returns results in 10 minutes, at most 2 per hour for the whole team, and only formal eval numbers count. You have 48 hours.

What I care about: don't be fooled by good single-seed results; before every formal submission, say what score you expect, and compare afterward;
what you hand me at the end is one policy, not five candidates; you decide which approaches deserve more people, but leave a trail.

Align first: which approaches you plan to split the work into, how many people on each, how you'll divide the slow-tier quota, and under what circumstances you'll adjust.
```

**Roughly how the 48 hours might go.** This is not a prediction; it is meant to give the reader a picture. In the first two hours the PI aligns with the human and splits out seven or eight approaches, with 1 to 2 workers each. Workers first read the profiles of their sites and run the fast tier locally. Around hours 5–6, the backfill approach sees a big improvement on site 1; the slow tier confirms it, and it is broadcast. The PI moves an idle worker over to tune its parameters. Around hour 10, someone tries extreme shortest-job-first: it looks great in the fast tier, violates the constraint in the slow tier, and is recorded on the blackboard as a dead end. After that no one submits it again. From hour 20, combining begins. Someone opens a "composition" task that merges the ingredients for four sites into one policy, finds that backfill and fair-share quotas conflict, and records the trade-off on the blackboard. The last 8 hours are only verification and closeout, and one policy is delivered. Every one of these steps should be visible in the recorder.

## 4. Problem B: a harness deliberately broken in five places

**What it is.** A coding agent harness of a few hundred lines: a system prompt, three or four tools (read file, write file, run tests), a main loop, context trimming, and retries. The model is fixed to haiku. The task set `tasks/dev` has 60 tasks. Each is a small Python project plus a failing test, and the goal is to make the test pass. `tasks/holdout` has another 60 tasks that workers can't see, used only for the final verdict.

**What may be changed.** Any of the harness's code and prompts. The model may not be swapped, and nothing task-specific may be hard-coded into the harness (this rule is backstopped by human review plus the holdout).

**The five planted bugs.** Start from a reasonably good harness and break one thing in each of five different dimensions. Each planted bug looks like a real bug or an oversight:

| Planted bug | What it looks like | Dimension |
| --- | --- | --- |
| Misleading tool description | The description of `run_tests` says "runs only the specified file", but it actually runs everything, and the agent misjudges because of it | Tools |
| Trimming cuts in the wrong place | When the context fills up, it cuts from the end, which happens to remove the last few lines of the error stack, the most useful part | Context management |
| No retry | When a tool fails intermittently (we inject 5% random failures), it just gives up | Flow |
| One bad instruction | The system prompt says "don't read files longer than 100 lines" | Prompt |
| Parsing bug | A multi-file diff applies only the first file | Code |

The pass-rate difference before and after breaking it is "100% of the available improvement", expected at 15 to 25 points and confirmed by the precheck. Traps and conflicts don't need to be planted here; they appear naturally. Stuffing hints about libraries common in the dev tasks into the prompt raises dev but not holdout, which the scorecard's "generalization" row catches. "Read more files" and "trim more aggressively" naturally conflict.

**Noise is this problem's number-one enemy.** With 60 tasks and a 40% pass rate, the standard deviation of a single eval is about 6 points. With nothing changed, a 10-point gap between two runs is normal. Three countermeasures. First, the slow tier returns per-task results, and every comparison is paired against the baseline (did the same task pass or not); pairing removes most of the noise. Second, the slow tier runs 60 tasks × 2 passes each time. Third, the fast tier runs a fixed 15-task subset once: 5 minutes, local, unlimited.

**Two eval tiers.** Fast tier: 15-task subset, local, about 5 minutes, unlimited. Slow tier: 60 tasks × 2 passes, 30 minutes, 2 per hour for the whole team. It returns the paired improvement over the baseline with its interval, plus a per-task breakdown.

**Money.** One haiku agent run on one task costs roughly a few cents to twenty cents, so one slow-tier eval of 120 runs costs about $5 to $25. 48 hours allow at most 96 slow-tier evals; adding the fast tier, one run of one arm comes to roughly $500 to $2,500. Three arms, two runs each, total a few thousand to over ten thousand dollars. This is why B comes after A.

**Ingredient detectors.** One probe test per planted bug: construct a situation that will hit that bug, run the harness, and see whether the bug is still there. After running the 5 probes you know how many were fixed.

**Precheck.** Run the original and the bugged version 3 passes each. The gap must be at least 3× the paired noise, and fixing each of the 5 planted bugs alone must give a visible gain.

**PI prompt.**

```
harness/ is our coding agent; the model is fixed to haiku and may not be swapped. tasks/dev has 60 tasks; the current pass rate is 41%.
Goal: raise the pass rate. The final verdict uses 60 holdout tasks you have never seen.

Locally you can run the fixed 15-task subset in tasks/dev as much as you like (about 5 minutes, noisy). The formal eval runs all 60 tasks twice, 30 minutes,
at most 2 per hour for the whole team, and returns a paired comparison against the baseline; only its numbers count. You have 48 hours.

Do not hard-code anything task-specific into the harness.

What I care about: for every change, explain clearly why it works; if dev goes up and holdout doesn't, I'll treat it as overfitting;
don't be fooled by a single 15-task result; before every formal submission, state the dev pass rate you expect, and compare afterward; at the end, deliver one harness, not five.

Align first: which dimensions you plan to change, how many people on each, and how you'll tell whether a change is a real improvement or noise.
```

## 5. Control groups and how to judge

Three arms, with the same problem directory, the same PI prompt, the same formal eval quota and the same time:

| Arm | What it is | The hypothesis it represents |
| --- | --- | --- |
| Single agent | One agent with the entire quota to itself; its qualifying candidates are also merged in by the merger (it can see its own baseline) | "One strong agent is enough" |
| Isolated | N agents with the same prompt, unable to see each other, quota split N ways; qualifying candidates are merged into the baseline during the run by a merger that is the same one, with the same configuration, as the herd's, but the agents don't receive baseline advances and keep working on the original baseline throughout; the arm is judged on the merger's last baseline version; the best single submission is reported separately as a diagnostic | "Parallelism is enough; no communication is needed" |
| herd | N workers plus a PI and a blackboard | Our design |

N equals the herd's number of workers. Each arm runs 2 times, because of noise.

The verdict looks at two things: the final score on holdout (report the effect size and the paired interval; no hard bar such as "win 3 out of 3"), and the §6 scorecard.

How to read the results. This is where, after a failure, you learn what to change:

| Score | Behavior | Meaning | Next step |
| --- | --- | --- | --- |
| herd wins | Meets target | The design holds | Move to problem B, then to a real project |
| herd wins | Misses target | A suspicious win; maybe more agents just made more attempts and got lucky | Check the recorder to see where the score came from |
| Tie or loss | Meets target | The herd behaves correctly, but "combining" doesn't carry enough weight in the problem, or the control groups are too strong | Change the problem: make the ingredients more composable; don't change the herd |
| Tie or loss | Misses target | See which row is red | Low breadth → the PI's prompt for splitting approaches; little composition → composition tasks didn't trigger; dead ends re-hit → blackboard reads or dead-end broadcast; poor quota discipline → pre-submission prediction didn't work; no tilt → the allocator; poor generalization → overfitting to the fast tier |

Each arrow in the last row points to a specific part of the herd design doc. After fixing it, rerun the same problem.

## 6. Scorecard

| Metric | How it is computed from the recorder | Target for A | Target for B |
| --- | --- | --- | --- |
| Breadth | Number of distinct approaches that reached the slow tier within the first 25% of the time | ≥ 5 (single agent about 2 to 3) | ≥ 4 |
| Composition | Number of ingredients detected in the final deliverable | ≥ 3 | ≥ 3 |
| Share of available gain captured | (final − baseline) ÷ (reference − baseline), see 11.5 | ≥ 70% | ≥ 60% |
| Dead-end re-hits | Number of times an idea marked as a dead end was submitted to the slow tier again by someone else | 0 | 0 |
| Conflict handling | Whether the two conflicting ingredients are both on; or whether there is a trade-off record on the blackboard | No, or there is a record | N/A |
| Quota discipline | Among slow-tier submissions, the share whose fast-tier result was already positive | ≥ 80% | ≥ 80% |
| Prediction error | Difference between the score predicted at submission and the actual score, second half vs. first half | Shrinks | Shrinks |
| Tilt | Within 2 hours after a big improvement appears in an approach, whether its number of workers increased | Yes | Yes |
| Generalization | Score difference between hidden data and public data | Less than the noise | Less than 1× the paired noise |

The thresholds are guesses. Calibrate them after the first run.

## 7. Build order

| Day | What to do | What you can see at the end |
| --- | --- | --- |
| 1 | Simulator, load generation for the 6 sites, baseline policy, fast-tier command | A score produced locally within one minute |
| 2 | Slow-tier service (queue, quota, hidden traces, 20 seeds, intervals), recorder, ingredient detectors, scorecard script | Submit a policy, get the result 10 minutes later, and see one line in the recorder |
| 3 | Precheck: reference policy vs. baseline; then run the "single agent" arm for 24 hours | All four precheck conditions pass; the single-agent arm's scorecard |
| 4 | Merger (11.8.1): FIFO, rebase, paired remeasure, acceptance rule, spot check, revert, closeout; then run the "isolated" arm for 24 hours | The merger's scripted tests are all green; scorecards for both control arms |
| 5 to 6 | Hook up the herd, run A, twice | Three-arm comparison table |
| 7 to 8 | Problem B: harness, planted bugs, task set, probes, precheck | Gap between the original and the bugged version confirmed |
| After that | The herd runs B |  |

Hand-off to the herd design doc: "Week 1" in §14 of the herd design doc starts after day 4 here (once the merger is built). The eval that the herd's week 1 needs is the slow-tier service here.

## 8. Defaults we are going with for now

None of the following is settled yet. For now we take the cheapest option; none of them is expensive to change:

| Item | Default | Why | Cost to change |
| --- | --- | --- | --- |
| Policy form | Code + parameters | Parameters alone give too few approaches to support "many approaches" | Small |
| Simulator | Our own, in SimPy, about 500 lines | Lets us plant structure and control noise | In phase two, swap in a realistic simulator such as Batsim |
| Simulator source | Readable by workers | In real life engineers can see it; the only hidden thing is the formal eval's traces | Small |
| Budget | 48 hours; slow tier 2 per hour for the whole team; fast tier unlimited | Consistent with the PI prompt | Change the numbers |
| B's model | haiku | Cheap; what we are testing is the harness, not the model | Change one line |
| B's task set | Half drawn from a public dataset of small Python bugs, half made by an LLM, all with tests | Fast | Swap the task set |
| Slow-tier latency | Artificially stretched to 10 minutes | Simulates queueing | Change one number |
| N for the control groups | Equal to the herd's number of workers | Fair | Change one number |
| Runs per arm | 2 | Noise | Money |

## 9. Known pitfalls

- Agents exploit loopholes in the simulator: countered by hidden traces, the subprocess sandbox, and the per-step time limit; in addition, a slow-tier score abnormally higher than the reference policy's triggers an automatic review
- The planted bugs are seen through: they must look like real bugs, with no comments; squash the git history after planting them
- Noise swamps the signal: this is what the day 3 and day 7 prechecks are for; if they fail, don't go further
- Quota contention inside the herd: this is the herd's own problem to solve (PI allocation); the scorecard's "quota discipline" row will expose it
- The fast tier overfits the public data: the scorecard's "generalization" row
- Cost runs away: the slow tier has a hard cap
- The "isolated" definition is unfair: they get the same PI prompt and the same total quota, and their qualifying candidates are merged in by the same merger as the herd's. The merger is identical across arms, so only if the herd beats this arm does it show the win comes from sharing, not from the merger
- The single-agent arm beats the herd: that is a conclusion in itself. First look at the scorecard for the reason; don't rush to make the problem bigger to "let the herd win"

## 10. What open source to reuse

| What needs solving | What to use | Notes |
| --- | --- | --- |
| Simulation | SimPy | A few hundred lines is enough |
| Slow-tier service | FastAPI + Redis | Queue and quota |
| Running the three arms, logging, viewing replays | Inspect AI (optional) | The first version can use just jsonl plus a script; bring it in when we need to view transcripts side by side |
| Realistic simulator (phase two) | Batsim | Realistic, but we can't plant structure in it |
| Don't use | Table-lookup benchmarks such as HPOBench | The policy space is too small; not enough approaches |

## 11. Spec for implementers

This section is for the agent that builds the testbed. Details left open earlier are settled here. Where this section differs from earlier ones, this section wins (11.5 is the only authoritative definition of the scorecard; the table in §6 is just a summary). Anything not yet settled here, the implementer decides and writes into `DECISIONS.md` at the repo root (one line each: what was decided, why, and whether it is expensive to change); don't come back to ask. The scorecard definitions and the three anti-cheating rules must not be changed on your own. All time units are seconds; timestamps are epoch seconds.

### 11.1 Repo structure

A standalone repo, `herd-testbed`, with no dependency on the herd repo. The herd will later connect to it only through HTTP and a few command-line tools.

```
herd-testbed/
  README.md  DECISIONS.md
  testbed/
    problem_api.py     # Problem protocol (11.2)
    slow_eval/         # slow tier: FastAPI; queue, quota and run registry in Redis (sqlite is fine for single-machine dev)
    cli/               # tb fast / submit / status / history / final / pack-problem / run
    recorder/          # event schema, append-only writes
    scorecard/         # metrics.py: one function per metric; report.py: three arms side by side
    sandbox/           # subprocess wrapper for running candidates; B's test runner is here too (not in the candidate)
    arms/              # single.py  isolated.py  herd.py (hooked up later)  scripted/
    integrator/        # merger core (11.8.1): shared by all three arms; the herd's Integrator calls it too
  problems/
    sched/             # problem A: sim/  sites/  policy/{baseline.py,params.yaml}  map/  reference/  probes/  hidden/  problem.yaml
    harness/           # problem B: harness/  map/  tasks/dev  tasks/holdout  planted/  probes/  problem.yaml
  tests/
```

`hidden/`, `reference/`, `tasks/holdout`, `planted/`, each task's `tests/test_hidden.py`, the `problem.yaml` on the slow-tier machine (which contains the hidden seeds), and the ingredient detectors in probes/ exist only on the slow-tier service's machine. The problem directory sent to workers is generated by `tb pack-problem`. It excludes all of the above, and adds `baseline_scores.json` (the baseline's fast-tier scores on public seeds 1 to 5) and map/. map/ holds the first batch of public measurement scripts: for A, wait time and p99 broken down by site and by job class; for B, failures broken down by task class. These are the initial scripts for the design doc's D76 opportunity map; they read the fast tier's per\_item output and write gap.md. One test asserts that the exclusion is clean. There is also problems/stub/, used only for the scripted self-check in 11.9: evaluate returns a fixed score based on the ingredients declared in the manifest (score = −1000 + 100 × number of ingredients; constraint\_ok = false when trap\_sjf is included), probes read the manifest directly, and the reference = all five ingredients on.

### 11.2 Problem pack protocol and candidate format

```python
class Problem(Protocol):
    name: str
    ingredients: list[str]      # controlled vocabulary. A = [backfill, admission, locality, packing, est_order, trap_sjf, fairshare]
    public_seeds: list[int]     # A = [1,2,3,4,5]
    def evaluate(self, candidate_dir: Path, tier: Literal['fast','slow'], seed: int, timeout: bool = True) -> Result: ...
    def aggregate(self, results: list[Result]) -> Agg: ...            # multiple seeds or passes -> one number
    def constraint_ok(self, agg: Agg, baseline_agg: Agg) -> bool: ...
    def probes(self, candidate_dir: Path) -> dict[str, bool]: ...     # ingredient name -> present or not; the keys are exactly ingredients

Result = {'score': float | None,                         # None = void (timeout or crash); the reason is in extra
          'per_item': [{'id': str, 'value': float}],     # A: site1.mean, site1.p99, ...; B: task id -> 0 / 0.5 / 1
          'extra': dict}                                 # A: timed_out, timeout_ratio; B: tokens
Agg    = {'score': float | None, 'ci': [lo, hi], 'n': int, 'per_seed': [float], 'per_item': [...], 'extra': dict}   # per_seed: the score per seed (per pass for B), so the paired difference between any two candidates can be computed after the fact without spending more T2
```

Higher `score` is better. A's score = −(equal-weight average of the six sites' mean wait, in seconds); B's score = pass rate. `ci` is always the 95% bootstrap interval of score itself (A resamples by seed, B by task). B additionally gives `delta_vs_baseline: {mean, ci}` in extra (paired per task).

A candidate = a directory. For A it is `policy.py` + `params.yaml`; for B it is the whole `harness/`. `tb submit <dir> --predict X --note ... [--parents h1,h2] [--direction label]` generates and overwrites `manifest.yaml` (actor is taken from an environment variable) and packs it into a zip (5 MB limit). hash = sort the files by relative path, concatenate `path\0bytes` for each file, append the identifier of the target baseline, and take the sha256 of the whole, ignoring `__pycache__`, `*.pyc` and the manifest itself. `direction` is for display only; an "approach" in the scorecard is always the ingredient set computed by the probes.

### 11.3 Slow-tier service

```
POST /runs            {run_id, arm, problem, N, overrides?}      # the arm runner calls this first; the slow tier writes a calibration event
                      every request after this must carry the header  X-TB-Run: <run_id>; quota, queue and records are all separated per run
POST /submit          {zip_b64, dataset?: dev|audit|judge}                                    # actor, prediction and parents are all in the manifest
  200 {ticket, position, visible_at}
  200 {ticket, duplicate: true, of: <ticket>}      # a ticket with the same hash already exists (including one still queued): no quota charged, records submit(duplicate=true); under per_actor_share, both duplicate and what /status shows are keyed by (actor, hash)
  429 {reason: 'quota', next_slot_at}
  400 {reason: 'invalid_candidate' | 'too_large' | 'missing_prediction'}
GET  /status/{ticket} {state: 'queued'|'running'|'done'|'failed', result?: Agg + {constraint_ok}}
                      no result is returned before visible_at
GET  /history         {quota: {per_hour, used_this_hour, total, used_total}, items: [...]}
                      items fields: ticket, actor, ts_submit, visible_at, hash, predicted_score, direction, note,
                      score, ci, constraint_ok (these three are null before visible_at). Under per_actor_share, only this actor's items are returned
GET  /meta            {baseline: Agg (the baseline on the hidden seeds; safe to expose), public_seeds, constraint_rule}
POST /events          {events: [...]}                              # accepts any type; appended to this run's recorder
POST /final           {hash | ticket}                              # called by tb final; under per_actor_share it is recorded as actor_final, and the run-level final is written by the runner at the end; a candidate with no result gets one extra run outside the quota
```

**Quota.** Fixed windows; window start = the time of `/runs`. Each full-hour window gives the whole team 2 submissions, with a total cap of 96; the first window already has 2. With `per_actor_share: true` (the isolated arm), it becomes 1 per actor every ceil(N/2) hours, total ceil(96/N). dataset defaults to dev. Only the arm runner can submit audit and judge; each has its own cap (audit: 10 per run), doesn't count against the 2 per hour, and uses its own traces (four sets: public, dev, audit, judge; same generator, different seeds). `visible_at = max(ts_submit + visible_delay_s, the time the computation finishes)`.

**Evaluation.** A: run `evaluate(tier='slow')` once on each of the 20 hidden seeds. The hidden seeds are 20 random 64-bit numbers, written only in the `problem.yaml` on the slow-tier machine; the fast tier accepts only public seeds 1 to 5. B: run the 60 dev tasks, 2 passes. When the service starts, for each problem it runs the baseline and the reference (A) or the clean version (B) once each on the hidden data and caches the results. The baseline's Agg is used for constraint\_ok and `/meta`. The reference's Agg is written only into the calibration event and is never returned by any endpoint.

**Adjustable time knobs** (defaults in `problem.yaml`; can be changed through the `overrides` of `/runs`): `quota_per_hour=2`, `quota_total=96`, `visible_delay_s=600`, `slow_seeds=20`, `tilt_window_s=7200`. There is no token cap: tokens are not something we are trying to save, and they don't count toward the scorecard. The merger's remeasure quota: quota\_integrator\_total=16, quota\_integrator\_per\_hour=1, the same for all three arms (11.8.1). The testbed self-check (11.9) uses `slow_seeds=2, visible_delay_s=1, quota_per_hour=100, tilt_window_s=10`.

**Command line.** `tb fast <dir> --seed N` (only 1 to 5 allowed; prints the score and the baseline's score on the same seed), `tb submit`, `tb status`, `tb history`, `tb final <hash|ticket>`. They take their values from the environment variables `TB_RUN_ID`, `TB_ARM`, `TB_ACTOR`, `TB_SLOW_URL`. `tb fast` POSTs a fast\_run event to `/events` and also appends it to `~/.tb/events.jsonl` as a backup. These are the interfaces the herd's Eval service will call later.

### 11.4 Recorder

One JSON object per line, append-only. The main file lives on the slow-tier machine, one file per run:

```json
{"ts": 1760800000.0, "run_id": "r-2026-10-21-a", "arm": "herd", "actor": "worker-07", "type": "...", "data": {...}}
```

| type | Written by | data |
| --- | --- | --- |
| calibration | slow tier, at `/runs` | baseline\_slow: Agg, reference\_slow: Agg (for B, the clean version), baseline\_fast: {seed: score} |
| fast\_run | `tb fast` | hash, seed, score, baseline\_score |
| submit | slow tier | ticket, hash, predicted\_score, parents, duplicate, direction |
| result | slow tier, ts = visible\_at | ticket, hash, score, ci, constraint\_ok, ingredients (the slow tier runs probes on every candidate) |
| quota\_rejected | slow tier | actor |
| blackboard\_write | herd tool layer | kind: one of claim, dead\_end, insight, compose; id; refs: list of candidate hashes; hash (required for dead\_end) |
| blackboard\_read | herd tool layer | ids |
| fork | herd tool layer | from\_actor, to\_actor, reason |
| allocation | herd tool layer | hash (the candidate that triggered this adjustment; required), workers\_before, workers\_after, reason |
| abandon | herd tool layer | hash, reason |
| final | slow tier, at `/final` | hash, ticket |
| final\_check | arm runner | dataset: public for A (seeds 1 to 5), holdout for B; score, ci |
| actor\_final | slow tier, /final under per\_actor\_share | actor, hash, ticket; the scorecard doesn't use it, only the run-level final |

T = the duration from `/runs` to the final event. The single-agent and isolated arms have no blackboard events, so the corresponding scorecard rows show "N/A". Merger events (baseline\_advanced and so on) exist in all three arms.

### 11.5 Scorecard algorithms (authoritative definition)

Each metric is one function in `scorecard/metrics.py`. Its input is all the events of one run, and it reads nothing else. cal = the calibration event.

| Metric | Algorithm |
| --- | --- |
| Breadth | Number of distinct ingredients appearing in result events with ts < 0.25T; all 7 count |
| Composition | Number of ingredients of the result that final points to, not counting trap\_sjf |
| Share of available gain captured | (final.score − cal.baseline\_slow.score) / (cal.reference\_slow.score − cal.baseline\_slow.score) |
| Dead-end re-hits | For each result with constraint\_ok=false or score < cal.baseline\_slow.score, count the submits after it with the same ingredient set (any actor, including the same one, including duplicates). For the herd arm, also add: after a dead\_end is written, the number of submits whose ingredient set equals that of the result its hash points to |
| Conflict handling | Whether final's ingredients include both backfill and fairshare; if they do, whether there is a blackboard\_write of kind insight or compose whose refs' corresponding results have an ingredient union that contains both |
| Quota discipline | Among submits, the share for which an earlier fast\_run with the same hash exists and its score > that entry's baseline\_score |
| Prediction error | Median of \|predicted\_score − score\| / \|score\|, split into first and second halves by submission sequence number; "shrinks" = second half ≤ 0.8 × first half |
| Tilt | The first result r that is higher than the best result at that time by ≥ 1 ci width; whether within tilt\_window\_s after it there is an allocation whose hash's ingredients intersect r's ingredients and workers\_after > workers\_before; N/A for non-herd arms |
| Generalization | (seen − unseen) / final's ci width, target < 1. For A, seen = final\_check (public seeds), unseen = final; for B, seen = final (dev), unseen = final\_check (holdout) |

### 11.6 Problem A spec

**Entities.**

```
Machine: id, site, rack, cores=8, free_cores, last_user_by_time  (used by site 4)
Job:     id, site, user, submit_t, cores, true_dur(not visible to the policy), est_dur, rack_pref(only site 3 has it), state
State shown to the policy: now, site, queue (public Job fields), machines (id, rack, free_cores, recent_users: user -> most recent time that user's job started or ended on that machine), running (job id, machine id, user, elapsed time, est_dur),
         per_user_running, params, site_profile
```

**Scale.** Sites 1, 2, 3, 5 and 6 have 16 machines each (128 cores); site 4 has 4 machines (32 cores). Simulate 48 hours. The arrival rate is derived from target utilization: rate = cores × utilization / mean(cores × true\_dur). The six sites total about 20,000 jobs. One seed, with the six sites in parallel, must finish within 60 seconds; if it takes longer, scale down machines and arrival rate proportionally. Common parameters: cores distribution 1: 50%, 2: 25%, 4: 15%, 8: 10% (all 1 at site 4); 20 users per site, uniform, except at site 1; at all sites est\_dur = true\_dur × lognormal(0, 0.3).

| Site | Utilization and arrivals | true\_dur | Mechanism specific to this site |
| --- | --- | --- | --- |
| 1 | Poisson, 0.85 | Pareto α=1.5, minimum 10 minutes, maximum 12 hours | One big user accounts for 60% of jobs; the other 19 share the rest |
| 2 | Non-homogeneous Poisson: base 0.5; rate × 5 for 1 hour each at 9:00 and 18:00 | lognormal, mean 20 minutes, σ\_log = 0.8 | Overload slowdown: when busy cores > 90%, the remaining durations of all running jobs elapse at 1.5× |
| 3 | Poisson, 0.8 | lognormal, mean 30 minutes, σ\_log = 0.8 | 4 racks of 4 machines each; jobs have a rack\_pref; a job placed on another rack takes 3× as long |
| 4 | Poisson, 0.7 | uniform 5 to 15 minutes, cores=1 | Startup overhead: 30 seconds are added when a job starts on a machine, unless the same user ran on that machine within the last 10 minutes |
| 5, 6 | Poisson, 0.8 | lognormal, mean 30 minutes, σ\_log = 0.8 | None |

**Scheduling loop.** On every arrival, every completion, and every 60-second tick, run once for that site:

```
q = order(queue, state)            # returns a list of job ids; must be a permutation of queue; otherwise treated as a timeout: FIFO this time, counts toward the 1%
for job in q:
    if not admit(job, state): continue        # stays in the queue, wait keeps counting, asked again next time
    m = place(job, free_machines, state)      # returns a machine id or None; returning an id not in free_machines, or one the job doesn't fit on, is treated as a timeout: placed the baseline way this time, counts toward the 1%
    if m is None: break                        # head-of-line blocking: this is why the baseline doesn't backfill; a policy that wants to wait for the job's own rack has to bear the blocking itself
    start(job, m)
if event == arrival:
    for v in preempt(running, state): requeue(v)   # returns a list of job ids; preempted jobs rerun from the start
```

The policy is imported in a subprocess under `sandbox/`; missing hooks are filled in with the baseline's. Each hook call is charged subprocess CPU time. A call over 50 ms has its result discarded and the baseline's used instead. When the number of timeouts > 1% of total calls, the whole run gets score = None, extra.timed\_out = true. `evaluate(timeout=False)` turns this mechanism off, for deterministic tests. The subprocess gets no network, and its cwd is a temporary directory. It stays resident for the length of one evaluate, so hooks can keep state in module-level variables; the probes start a new subprocess for each scenario. When any seed's score is None, Agg.score = None, constraint\_ok = false, and extra.failed\_seeds lists the seeds. 11.5 treats score = None the same as constraint\_ok = false, so such a candidate is never chosen as final.

**Score and constraint.** wait = the sum of all of a job's queueing intervals (time requeued after preemption accumulates). score = −(equal-weight average of the six sites' mean wait). `per_item` = each site's `siteK.mean` and `siteK.p99`. aggregate pools all waits from the 20 seeds to compute each site's p99. constraint\_ok = every site's pooled p99 ≤ the baseline's pooled p99 + tolerance, where tolerance = max(2% of the baseline's pooled p99, the standard deviation of the baseline's per-seed p99).

**Baseline.** FIFO, admit everything, first-fit (the first machine, by machine id, that the job fits on), no preemption.

**Probes for the ingredients, the trap and the conflict.** A probe calls the hooks directly or runs one pass of the scheduling loop; it doesn't run a full simulation. Each probe uses 50 random instances and takes the proportion.

| Name | Where it earns | Probe (pass condition) |
| --- | --- | --- |
| backfill | Site 1 | Head of queue: 8 cores, 6 hours; second in queue: 2 cores, 5 minutes; only 4 cores free. Run one pass of the scheduling loop: the short one has started ≥ 80% |
| admission | Site 2 | Busy cores 95%, 300 in the queue, 500 more arriving within 1 minute: the proportion admit lets in immediately ≤ 0.5 |
| locality | Site 3 | rack\_pref=r, free machines both inside and outside r: place chooses inside r ≥ 90% |
| packing | Site 4 | m1 and m2 each have one job of u1 running, m3 and m4 each have one of u2; 16 one-core jobs arrive, alternating between u1 and u2: the number of jobs placed on a machine where the same user ran within 10 minutes ÷ 16 ≥ 90% (first-fit gets about 50% to 60%) |
| est\_order | Sites 5, 6 | 20 jobs with random est\_dur: Kendall τ of (output position, est\_dur) ≥ +0.5 |
| trap\_sjf | Trap | est\_order passes, and: one long job in the queue, 3 short ones arriving per minute, simulate 2 hours: the proportion in which the long job never starts ≥ 90% |
| fairshare | One of the conflict pair | User u already has 50 jobs running: order puts others ahead of u ≥ 90%, and admit rejects u ≥ 90% |

The baseline must fail all 7 (first-fit ignores users and racks; head-of-line blocking means no backfill; FIFO's τ is about 0). Conflict pair = backfill + fairshare (the strict version): at site 1 the big user accounts for 60% of jobs, strict fairness pushes that user's short jobs to the back, and so backfill has nothing to fill in.

**Reference policy.** `reference/policy.py`: backfill (window = the est\_dur of the job at the head of the queue) + admission (when busy cores > 90%, admit only the ones with the shortest est\_dur) + locality + packing (prefer machines where the same user ran within 10 minutes; new users prefer empty machines) + est\_order with aging (jobs that have waited more than 30 minutes are moved forward by wait time), with fairshare off. It must pass the 5 ingredient probes and fail trap\_sjf and fairshare.

**The four precheck conditions (day 3).** Baseline and reference, 20 seeds each: (1) (reference.score − baseline.score) / the baseline's per-seed standard deviation ≥ 5; (2) each of the five ingredients, turned on alone, improves mean wait at its own site by ≥ 1σ; (3) a pure SJF policy gets constraint\_ok=false in the slow tier; (4) backfill + fairshare is worse than backfill alone by ≥ 1σ. Write this as `tests/test_precheck_sched.py`; it is slow, so mark it and run it separately. If it fails, tune the generation parameters and write the before-and-after numbers into DECISIONS.md.

### 11.7 Problem B spec

**harness.** Python: `agent.py` (main loop, at most 25 turns), `tools.py` (read\_file, write\_file, list\_files, run\_tests), `prompts.py`, `context.py` (trims above 60k tokens), `model.py` (haiku, temperature 0). `run.py <task_dir> --out result.json`. `run_tests` must call the `tb-pytest` that the testbed installs in the sandbox image (not in the candidate directory). The 5% random TransientError is injected inside `tb-pytest`, so workers can't remove it.

**Tasks.** One task = `repo/` (50 to 300 lines) + `tests/test_visible.py` (currently failing) + `tests/test_hidden.py` (only on the slow-tier machine, used for scoring) + `task.md`. Generate 150: an LLM writes the module and tests, then one bug is injected (five classes, 30 each: boundary, sign, exception handling, call order, multi-file inconsistency). Keep a task only if: the original passes all tests; after injection, both visible and hidden fail; and the clean harness running haiku, 3 runs, has a pass rate between 30% and 90%. Stratify by bug type and draw 60/60 as dev/holdout.

**The five planted bugs.** Each is a patch in `planted/`. After all of them are applied, squash them into one commit.

| Planted bug | patch | Probe (pytest) |
| --- | --- | --- |
| tool\_desc | The description of run\_tests is changed to "runs only the specified test file"; the implementation is unchanged (it runs everything) | Call run\_tests with one test file specified and check whether only that file ran; or whether the description matches the behavior |
| trim\_tail | context.py is changed to cut from the end of tool output (originally it kept the last 40 lines) | Feed it a 500-line traceback; after trimming, the last 20 lines are still there |
| no\_retry | The retry on TransientError is removed from tools.py | Mock one failure followed by a success; number of calls ≥ 2 |
| bad\_rule | The system prompt gets "don't read files longer than 100 lines" | Static: this sentence, or an equivalent limit, is absent; behavioral: on a task that requires reading a 300-line file, run once, and read\_file read that file |
| diff\_first | The multi-file branch of write\_file applies only the first file | Two-file diff; both files changed |

**Evaluation.** `evaluate(tier='slow', seed=s)` runs the 60 dev tasks once, 30 in parallel; s is the pass number. The slow tier runs s = 1, 2. After aggregate: per\_item value = pass rate 0 / 0.5 / 1; ci is a per-task bootstrap; `extra.delta_vs_baseline` is the mean and interval of the per-task paired differences (baseline = the bugged version, cached at startup). constraint\_ok is always true for B. predicted\_score is the pass rate on the 60 dev tasks, as an absolute value from 0 to 1. Fast tier = the first 15 tasks, 1 pass. Each run is limited to 200k tokens; going over counts as failing that task. holdout runs only once, on final, run by the arm runner, which writes final\_check(dataset=holdout).

**Precheck (day 7).** Clean version and bugged version, 3 passes each: the paired difference ≥ 3× the paired standard deviation; fixing each of the five planted bugs alone gives a difference > 0; all five probes pass on the clean version and all fail on the bugged version.

### 11.8 Runners for the three arms

`tb run --arm single|isolated|herd --problem sched --hours 48 --workers N --run-id r-...`. What it does: `POST /runs`; `tb pack-problem`; fill in the PI prompt template (the passage in §3 or §4, with `{hours}` and the quota numbers as placeholders) and hand it, with the problem directory, to the agent, with `TB_*` in the environment variables. When the agent asks the "align first" question, the runner automatically replies: "No one is online. Proceed with your plan, and write the alignment into PLAN.md." Stop when time is up. If the agent didn't call `tb final`, the runner calls it on the agent's behalf. Run-level final = the merger's last baseline version (11.8.1); the agent's own final is recorded as actor\_final. Run final\_check. Run the scorecard. All arms and all roles use the same, strongest model, with no tiering.

- single: one Claude Code session (or Agent SDK), the whole quota; the merger runs as usual, and since the agent is itself the author, it sees the baseline advance
- isolated: N sessions, the same prompt, each with its own container and working directory, `per_actor_share`. The merger (11.8.1) merges each agent's candidates that pass the slow tier into this arm's baseline, FIFO. The agents don't receive baseline\_advanced and keep working on the original baseline throughout. Each agent's tb final is recorded as actor\_final; run-level final = the merger's last baseline version; the best single candidate is recorded separately as actor\_best, as a diagnostic. There is no longer a post-run greedy git-apply merge step: with that step, if the herd won, we couldn't tell whether it won through sharing or through the merger
- herd: calls the herd repo's entry point, passing the slow-tier address and run\_id; before day 4 it only needs to exist and report "not hooked up"

### 11.8.1 Merger (tb integrate, shared by all three arms)

A pure-code process, one per run, started together with the arm. All three arms use the same code and the same configuration. It maintains the run's baseline branch baseline@k (in the repo of the system being optimized, not the recorder), and for each version writes `baseline/<k>.json`: which candidates it contains, the paired results, and constraint\_ok. The herd's Integrator (design doc D74) is just this with a layer of blackboard events and a task queue wrapped around it; its core is not written separately. This is the only exception to "don't build any part of the herd", because the two control arms need it too.

**Input**: candidates for which the slow tier returned done with constraint\_ok=true, first in, first out by result arrival time.

**For each candidate**:

1. Convert it into a unified diff against baseline@0 (policy.py, which is a whole-file candidate, is diffed first; params.yaml is merged by key), and rebase it onto the current baseline@k.
2. Textual conflict → issue a compose task: open a new agent session that belongs to no arm, give it the two candidates and the original text, and have it produce a new candidate that goes back into the slow-tier queue (its hash includes the baseline identifier). The original candidate is recorded as integration\_failed(reason=conflict). All three arms do this; the agents of the isolated arm never touch it.
3. No conflict, and k is the baseline the candidate was validated against → use the score from that validation directly. k has moved → submit one paired remeasure against baseline@k (`/submit`, dataset=dev, manifest.target = baseline@k), using the merger's own quota.
4. Acceptance: paired gain (candidate minus baseline@k, mean of the per-seed differences) exceeds 1 standard error (the standard error of the per-seed differences) and constraint\_ok → merge, k+1, write `baseline/<k+1>.json`, and write baseline\_advanced{baseline, candidate, files\_changed, paired} to the recorder. Otherwise write integration\_failed(reason=below\_1se | constraint). Earlier gain minus remeasured gain exceeds 2σ√2 → also write an interaction\_found.
5. In problem A the same seed is deterministic, so each baseline version's per-seed scores already exist when it is merged and need not be measured again. Problem B involves model calls, so remeasures must really be run.

**Spot check**: after every 2 merges, take this segment (from j, at the last spot check, to the current k) and compute the per-seed difference baseline@k minus baseline@j once on the dev seeds and once on the audit seeds (the audit one uses dataset=audit and counts against the spot-check quota). If the two differ by more than 2.5·σ√2 → bisect the merges in this segment to find the culprit, write a new baseline version that excludes it (baseline\_reverted; old records are left unchanged), and mark the candidate quarantined. σ is computed during the day 3 precheck: the standard deviation of the reference policy's per-seed differences against the baseline, divided by √(number of seeds), one for dev and one for audit. Spot checks per run ≤ 10, keeping 3 for bisection and the final spot check. Why not compare against the gains measured at merge time? Those are measurements that were kept because they passed the gate, so they are biased high to begin with; in simulation they raise frequent alarms even when no one is cheating.

**Closeout** (when time is up or the quota is used up): stop accepting → final spot check → freeze the deliverable = the last baseline version → the runner uses it to call tb final and final\_check. Neither problem has an expensive gate in the testbed, so there is no guard\_pending, and the catch-up check step in the design doc's closeout order is empty.

**Quota**: the merger's remeasures use a separate share of the slow tier, quota\_integrator\_total=16 per run and 1 per hour \[Default\], the same for all three arms. The workers' 2/hour and 96 total are unchanged. When the quota runs out, the merger merges only candidates whose baseline hasn't moved; the rest wait in the queue for the next window.

**The arms differ in only one place**: whether baseline\_advanced is pushed to the agents. single: the agent is itself the author, so it sees it. isolated: not pushed; the agents keep working on baseline@0, and the merger merges in the background. herd: pushed.

**Tests** (scripted, no LLM): 3 mutually non-conflicting candidates → 3 baseline versions and 3 baseline\_advanced events; the 2nd candidate conflicts with the 1st → a compose task is issued and the 2nd is recorded as integration\_failed; a candidate whose baseline has moved → exactly one remeasure submission; gain under 1 standard error → not merged; a scripted candidate that is "good only on the dev seeds" → the spot check flags it, it is reverted, and it is quarantined; a clean run with 7 spot checks raises no alarm; the merger configurations of the three arms are equal field by field.

### 11.9 Acceptance of the testbed itself (no LLM)

`arms/scripted/` holds three scripted "fake herds". They run with the compressed knobs from 11.3 and problems/stub. Candidates' scores and ingredients are determined by the manifest. They finish in a few minutes, and red or green depends only on the script's behavior:

| Script | Scenario | The scorecard should show |
| --- | --- | --- |
| perfect\_herd | 6 actors; touch 5 ingredients in the first 25% of the time; after the trap is submitted once, a dead\_end (with hash) is written and no one submits it again; every submission is preceded by a positive fast\_run; after a big improvement there is an allocation with a hash; predictions are more accurate in the second half; final contains 4 ingredients and not fairshare | All green |
| lone\_wolf | 1 actor, two ingredients, submits the trap twice in a row | Breadth red, dead-end re-hits red, composition red |
| quota\_hog | 6 actors; submit without any fast\_run; all predictions filled in as 0 | Quota discipline red, prediction error doesn't shrink |

Also add these tests: the 3rd submission within one window gets a 429; `status` gives no result before `visible_at`; the same hash (including one still queued) charges no quota but records a submit; requests missing `X-TB-Run` are rejected; the output of `tb pack-problem` contains no hidden/, reference/, holdout, planted/, probes/ or test\_hidden.py; with `timeout=False`, two runs on the same seed give byte-identical results; one seed with the six sites in parallel takes < 60 seconds; the reference policy passes the 5 ingredient probes and fails trap\_sjf and fairshare; the baseline fails all 7; a policy with more than 1% timeouts gets score None.

### 11.10 Definition of done

At the end of each day in §7, the corresponding tests are all green, and the `README` has a copy-pasteable command that reproduces that day's output. Day 3 is done when: the numbers for the four precheck conditions are printed in the terminal and all meet target; and `tb run --arm single --hours 24` finishes on problem A and the scorecard has numbers. Day 4 is done when: the merger's scripted tests are all green; and the isolated arm with 5 workers finishes its 24-hour run, so the comparison table has two arms. At that point the testbed can be handed over to the herd side to hook up.

Don't: build any part of the herd (except the merger core, which is pure code, shared by all three arms, and lives in the testbed); add authentication, signing or a user system; build a web UI; replace SimPy to make things look nicer; adjust scorecard thresholds so that a failing precheck passes.

### 11.11 Starting prompt for the implementing agent

```
Read all of "Herd Testbed". §11 is your spec; where it differs from earlier sections, §11 wins. Build a new repo, herd-testbed,
following the order in §7. Do days 1 to 3 first (problem A and the testbed itself); don't start problem B until I tell you to.

Rules: decide anything the document leaves open yourself, write it into DECISIONS.md, and don't stop to ask; don't change the scorecard definitions in 11.5 or the three anti-cheating rules;
at the end of each day, the corresponding tests in 11.9 must be green; if the precheck fails, change the simulator parameters, and write what you changed and the before-and-after numbers into DECISIONS.md.
Don't use any LLM before the single-agent arm on day 3.

Every day, give me one paragraph: what got done, whether the tests are all green, which entries were added to DECISIONS.md, and what you will do the next day.
```

## Appendix: from "Ledger" to "testbed"

The previous version wrote this document as an eval trust layer: a threat model, a signed ledger, a circuit breaker, a replayer, 12 attack scripts, and moving the Critic into the trust zone. That was defending against cheating agents. The testbed's adversary is not a cheating agent but errors in our own design, so all of that was cut. What remained became the three rules in §2 and the recorder: hidden data is not on worker machines, every run gets a new repo, workers can't push, records are append-only, there are two sets of data (public and hidden), and you predict before you submit. Real anti-cheating is a matter for when we hook up a real project. We will add it then, and when we do, this recorder is the starting point.

The criteria for choosing the problem were changed as well. The previous version's test problem was 80 knobs plus case files, on the grounds that "the material exceeds one context". §1 explains why that reasoning doesn't hold.
