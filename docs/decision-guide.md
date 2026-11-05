# Decision guide: what blocks the H1 result, and how to resolve each item

Written 2026-11-01 for @minyi-tang. Each item below says why it exists, what the spec already fixes, what was open, the options with their trade-offs, and a concrete example. On 2026-11-05 @minyi-tang delegated the Part 1 decisions to Claude; each section now opens with what was decided, and the answers of record are in `docs/open-questions.md`. Overrule any of them by editing that file. Parts 2 and 3 are still proposals.

Where to record an answer: `docs/open-questions.md` (the Answer column, with the date), `testbed/DECISIONS.md` for testbed items, and the issue named in each section.

---

## Part 1. Decisions only you can make

### 1.1 The preregistration numbers (open-questions row 2; #124)

**Decided 2026-11-05 (by Claude, at your delegation):** N = 5 workers per arm; 24-hour runs on problem A with the service's own quota (48 worker submissions, 16 remeasures, 10 holdout checks, 5 verdict repeats); minimum effect 10 percentage points of captured gain on `holdout_verdict`; 3 to 6 batches from the pilot's power computation, exploratory if more are needed; default acceptance rule. Provisional: it is sealed only when you sign the preregistration file after the pilot. This supersedes the example below (48 hours, 0.5 reference-z): 24 hours is what TB§7 uses for the control arms and halves the calendar time, and captured gain is the scorecard's own unit.

**Why this exists.** The whole project is one experiment: does a sharing herd (S2) beat the same agents isolated and merged the same way (B1+M)? The judge is noisy, and the LLM API is not reproducible, so a single run proves nothing. D42 rule 4 and D72 say the comparison must be preregistered: the effect size, the number of runs, the budget and the test are written and sealed before the formal runs start. A comparison that was not preregistered "cannot be judged". Agents are forbidden from picking these numbers because whoever picks them after seeing data can make any result look significant.

**What the spec already fixes.** Most of the protocol is not yours to choose; D72 sets it:

- the data is cut three ways on day one: daily use, `holdout_run` for in-run spot checks, `holdout_verdict` for the final verdict, touched once after every run has ended;
- a pilot of 3 batches, each arm running in parallel inside a batch, measures the between-run noise σ_run;
- the number of runs is the smallest n with statistical power ≥ 80 percent, with σ_run inflated 1.5 times because 3 batches give only 2 degrees of freedom;
- the test is a one-sided paired t-test at α = 0.05, paired by batch, with the effect size and a 90 percent interval reported;
- if the budget does not cover n runs, the result is labelled exploratory and does not pass the gate.

**What is yours.** Three numbers and one choice:

1. **The minimum meaningful effect**, on `holdout_verdict`, in the problem's score units. D72 says this is set by a human and cannot come from the data. For problem A the score is a z-like number against the reference (the precheck rows talk about 1.01 and 2.19). For the prompt example it is a 0 to 1 grade.
2. **N, the workers per arm**: 5 or 10. The spec says "5 to 10, small before large". Five is cheaper and is what the roadmap's gate names. Ten shows more sharing but doubles API concurrency and eval load.
3. **Eval machine-hours per arm**, the budget each arm may spend, counting every source (D42 rule 2). On problem A the testbed enforces its own ceiling: 2 slow runs per hour for the whole team, 96 total over 48 hours, 20 seeds each.
4. **Acceptance rule**: default or strict version, fixed before the runs; no switching mid-run.

**How to choose the effect.** Ask what difference would change what you build next. If S2 beats B1 by less than judge noise you would not reorganize around sharing. A practical anchor: the pilot's σ_paired. An effect you want to detect should be at least about 1.5 to 2 times σ_run, or n becomes large (D72: at effect = 1.5σ, 3 runs give far below 80 percent power). You can set the effect provisionally now and confirm it after the pilot, as long as it is sealed before the formal runs.

**Example answer you could write today.**

> Arms: S2 (Planner off) vs B1+M, S1 as an intermediate diagnostic arm, not gated. N = 5 workers per arm. Budget per arm per run: 96 slow-tier runs on problem A, 48 hours wall clock (the testbed ceiling), fast tier unlimited. Minimum meaningful effect: S2's holdout_verdict exceeds B1+M's by 0.5 reference-z, provisional until the pilot; sealed before the formal runs. Runs: computed from the pilot per D72, at least 3 per arm. Test: one-sided paired t-test, α = 0.05, paired by batch. Acceptance: default version.

**Then.** Issue #124 builds the tooling that uses these: A/A remeasures for σ, pilot batches, the power computation, the t-test and a `vlah experiment check` that refuses to start without a signed preregistration file. It is unblocked today; the numbers slot into its template.

---

### 1.2 Which code runs the control arm (open-questions "design decisions", roadmap question 5)

**Decided 2026-11-05 (by Claude, at your delegation):** Option 1. The gated control arm is the herd runtime's B1 preset; the testbed's isolated runner may run beside a batch as an unpaired reference.

**Why this exists.** H1 compares two arms. They must differ in exactly one thing: whether workers see each other's work (D58, D74). There are two candidates for the B1 arm: the herd runtime with its B1 preset (`vlah run --arm B1`, filtered views, same tools, same prompts, same egress), or the testbed's own `isolated` runner, which starts plain Claude Code sessions that call `tb submit` directly.

**Options.**

1. **The herd runtime's B1 preset.** Same tool server, same worker prompt, same sandbox, same Integrator; the only switch flipped is the view filter. This is what D58 and D67 assume. It is already built, and the S2-versus-B1 pair in PR #114 ran this way. Cost: B1 workers carry the herd's tool surface even though they cannot use the sharing parts.
2. **The testbed's isolated runner.** Simpler, closer to "an agent with a CLI", and it is what a skeptic might call the real baseline. Cost: the two arms then differ in tools, prompts and egress at once. D58 exists to forbid exactly that confound.
3. **Both, with roles.** The herd's B1 preset is the gated control; the testbed runner is a reported-only sanity arm. This costs one more arm per batch.

**Recommendation.** Option 1 for the gate. Option 3 only if you want the "plain agent" number for the write-up.

**Example answer.** "The H1 control arm is `vlah run --arm B1`. The testbed's isolated runner may run alongside as an unpaired reference and is not part of the gate."

---

### 1.3 Problem A is too easy (#81), with #84 and #85 attached

**Decided 2026-11-05 (by Claude, at your delegation):** Ideas 1 and 3 first, then rerun the 30-minute acceptance run. Idea 4, with #84 and #85 as one retune, only if capture is still at or above 70 percent. The bar stays below 70 percent at 30 minutes. All before the pilot.

**Why this exists.** The scorecard's capture row asks whether the herd captures 70 percent of the reference gain. In a 30-minute single-agent run, one agent reached 84 percent in 6.6 minutes and 97 percent in 22; after the stage-two fix (hidden judge tier, PR #105 and #116) it still reached 82 percent. If one agent captures the target, the row cannot tell a herd from a loner, so the gate is blind on its most important row.

**Why it is easy** (from the issue's evidence): the site descriptions name each mechanism and its size; the public files carry the exact settings; `tb fast` prints per-site mechanism counters (cold starts, off-rack jobs, overloaded seconds) that tell the agent what to fix; and the local test predicts the official one almost exactly, so the agent needs no slow runs.

**What you already decided (2026-10-23).** Make the mechanisms less visible rather than change the score formula. The scorecard and anti-cheat rules are fixed (TB§11.5). The fast tier stays free and the same for every arm.

**Open: how far to go.** The recorded ideas, which combine:

1. **Symptom descriptions.** Site text says what users complain about, not which mechanism or number causes it. Cheap; changes only `problems/sched` text. Removes the hint but not the counters.
2. **Hide the generator and mechanism settings** under `private/testbed/sched/` and run `tb fast` server-side. Already half done by stage two. Removes the "exact copy of the judge" problem (#82) completely. Cost: the fast tier can no longer run inside the worker's sandbox; it becomes a service call, which is also what the herd arm wants (#115).
3. **Drop the mechanism counters** from the fast tier's output. Agents then have to infer mechanisms from wait and p99 by site, which is the research task.
4. **Let the public settings drift from the hidden ones**, so the local test is a biased guide. This is what a real benchmark looks like, and it is the only idea that makes the slow tier worth spending. Cost: precheck row 4 is at 1.01 against a threshold of 1 (#84); any retune can break it, and the trap's p99 regression is visible locally (#85), which weakens two more rows.

**Recommendation.** Do 1 and 3 now; they are text and output changes with no retune. Then 4, together with #84 and #85 as one retune, because every retune costs a precheck and a 30-minute acceptance run. Keep the acceptance check the reviewer proposed, below 70 percent capture at 30 minutes, unless you want a different bar.

**You decide:** the acceptance number (70 percent at 30 minutes, or something else), and whether option 4 is in scope now or after the first comparison.

---

### 1.4 Site 4's cold start counted as waiting time (#83, deviation A14)

**Decided 2026-11-05 (by Claude, at your delegation):** Option 1, keep A14 as a flagged deviation. The literal form is tried only inside a #81 retune, and replaces the deviation only if precheck rows 2 and 6 still pass.

**Why this exists.** The testbed spec defines wait as the sum of a job's queueing intervals and puts start-up cost on run time. During the composability retune an agent changed site 4 so that a cold start adds 720 seconds to wait, because otherwise the "packing" ingredient (keep machines warm for returning users) was worth 0.3 percent of the gain and failed precheck row 6's 8 percent floor. With the penalty it is worth about 20 percent. The change was proposed by an agent, not decided by you; you skipped the question on 2026-10-23.

**Options.**

1. **Keep A14 as a flagged deviation.** No work. The formula is unchanged in form, but "wait" at site 4 is not only queueing. A reader of the write-up must be told.
2. **Go literal and retune site 4** so packing pays through load: heavier load so cores held during preparation cause real queueing. Rows 2 and 6 must pass again. Pairs well with the #81 retune.
3. **Go literal and drop packing** from the ingredient list. Changes the list TB§11.6 fixes, so it is the least attractive.

**Recommendation.** Option 1 now, recorded in `testbed/DECISIONS.md`, with option 2 folded into the #81 retune if that retune happens before the comparison.

---

### 1.5 Trust and visibility of each source, and where the private store lives (open-questions row 6)

**Decided 2026-11-05 (by Claude, at your delegation):** `slow`: `trust: 0.9`, `readable_by: [critic]`. The machine-hour cost is measured during #124's A/A remeasures rather than taken from the 10-minute hold, because the charter already says the artificial delay is not machine time; until then the budget is stated in runs. Private store: the eval host's directory. Credentials as built. This supersedes the example below (trust 1.0, cost 0.17).

**Why this exists.** Every eval source declares `trust` (0 to 1; sets its ladder position and whether it may move permanent budget) and `readable_by` (who may read its definition; if `worker` is in the list the definition lives on the blackboard, otherwise in the private store, D10 and D63). Git has no per-path read permission, so anything a worker must not see cannot be in the blackboard repo at all. The spec says these are mandatory questions for the human (D45), because they decide whether anti-hacking measures hold: if workers can read the judge, every measure fails.

**What exists today.**

- `examples/prompt_optimization`: one source `rubric`, `trust: 0.9`, `readable_by: [critic]`, judge code under `private/`, 1000 runs per day, 0.05 machine-hours each. Signed.
- `examples/testbed-sched`: one source `slow`, `trust: 0`, `readable_by: []`, `cost_machine_hours: 1`, 48 per day. These are schema-only sentinels, not answers, and the charter stays `draft` until you replace them.
- The private store in the PoC is a directory on this machine (`~/.vlah-private/testbed/sched` for the testbed; `private/` inside the problem folder for the example), reachable only by the eval service. Workers in Docker and kind have no path to it; the boundary probe checks this.

**What is yours.**

1. For the testbed `slow` source: `trust` (0.9 or 1.0 is the natural value for the official judge), `readable_by` (`[critic]` or `[]`; never `worker`), `cost_machine_hours` (the slow tier computes in 1 to 2 minutes but is held to 10; count the held time or the compute time, and say which).
2. **Private store form.** A directory on the eval host (today), a separate git repository, or an object-storage bucket. For one machine and the local stack the directory is enough; the bucket matters only on a real cluster (Wave 3).
3. **Credential isolation.** Today: workers get a per-session read token for their served view and no push token; only the tool server pushes (D68); the eval service alone reads `private/`. Confirm this is the policy, or ask for more.

**Example answer.** "`slow`: trust 1.0, readable_by [critic], cost_machine_hours 0.17 (the 10-minute hold). Private store: the eval host's directory for the PoC and the local stack; a bucket when a cluster exists. Credentials as built: per-session view tokens, no worker push, eval service only for private."

---

### 1.6 Who writes `spec.md` and the first approaches, and how many slots (open-questions row 4)

**Decided 2026-11-05 (by Claude, at your delegation):** The checked-in scheduler drafts are adopted, identical in every arm. Slots at N = 5: `order` 2, the other three 1 each, so every worker has a slot; ordering carries both backfill and fair-share.

**Why this exists.** In the H1 phase there is no PI agent and no Planner (D42 rule 1: allocation is FIFO plus a fixed quota in every arm). Someone has to write the problem statement and the starting directions, and fix how many workers each direction gets, before the run. In a real project this is the human and PI agent's alignment dialogue; for the comparison it must be the same text in every arm.

**What exists.** `examples/testbed-sched` has one active approach and one slot per public policy hook (`order`, `admit`, `place`, `preempt`), with questions drawn from the PI prompt and the policy API. `examples/prompt_optimization` has four approaches with one slot each. Both were written by agents in sessions you supervised.

**Options.**

1. **You write them**, or edit the drafts. Strongest claim of independence from the system under test.
2. **An agent drafts, you sign.** What has happened so far. Fine as long as the signed text is frozen and identical across arms.
3. **Slots:** with N = 5 and four approaches, one slot each plus one free worker; with N = 10, two each plus two free. "Free" means the worker chooses an approach from the list.

**Example answer.** "The testbed-sched `spec.md` and the four hook approaches as drafted, reviewed and signed by me on <date>; slots_per_approach 1 at N = 5. The same files in every arm."

---

### 1.7 API concurrency and rate limits (open-questions row 3, second half)

**Decided 2026-11-05 (by Claude, at your delegation):** No limit is configured. Every arm of a batch runs at once, 10 worker sessions at the peak. Load the gateway at that peak for 10 minutes before the pilot; if it throttles, drop S1, then fall back to back-to-back arms on the same day, recorded as a deviation.

**Why this exists.** P10 says tokens are not a constraint, but concurrency can be: N workers per arm times the arms in a batch, plus the model-graded judge's calls, all through one LiteLLM gateway in front of GitHub Copilot. If the gateway or the provider throttles, workers stall and the wall-clock budget is spent waiting, which biases whichever arm makes more calls (S2, which reads more).

**What to decide.** The gateway's parallel-request limit and the provider's per-minute limits as you know them, so the runtime can size batches: how many arms run at once in a batch (D72 wants all arms of a batch in parallel), and whether the judge's model calls share the same gateway.

**Example answer.** "Gateway: up to <k> concurrent requests. A batch runs S2 and B1 at the same time, 5 workers each, so 10 worker sessions plus the judge's grader; if the gateway cannot hold that, run arms within a batch back to back on the same day and record it as a deviation."

---

### 1.8 Three design questions from the one-page alignment (§6)

**Decided 2026-11-05 (by Claude, at your delegation):** Yes to all three.

These have waited since 2026-10-22. None blocks code, but each is written into the plan as "pending your approval".

1. **P11, govern the boundaries, not the paths.** The harness enforces only: eval quota and queue, dedup by hash, append-only records, the same merger in every arm with its gates and paired remeasure, invisible eval definitions, the egress allowlist, the lease as a declaration, and predictions on submissions. Everything about how agents explore, what they read and when, and what they do after failure is a prompt suggestion. New mandatory rules are added one at a time, each in response to a scorecard item that lost to "isolated". Saying yes downgrades a few design-document rules to suggestions. Saying no means building and enforcing those rules before the comparison. The code so far was built as if yes.
2. **Zero LLM roles in the minimal herd.** No Librarian, Planner or Scout until the scorecard shows one is needed. The design document said the PI and Librarian exist from week one. The roadmap already defers the Librarian; at five workers agents read each other's candidates directly. Saying yes makes this official and removes the Librarian from Wave 2. Saying no adds a component before H1.
3. **Problem C (the herd improving itself) last.** Step six, after the real project. PR #31 proposes the CI design for when it comes. Saying yes costs nothing now.

**Recommendation.** Yes to all three, as the code and roadmap already assume. Record the dates in `docs/open-questions.md`.

---

### 1.9 The H11 checkpoint proposal (#24)

**Decided 2026-11-05 (by Claude, at your delegation):** Deferred past the H1 gate; the seven sub-decisions reopen with H2. #24 can close as delivered.

**Why this exists.** D5 (fork instead of handoff) and D22 (the first wave of workers forked from the PI's context) assume an agent's conversation and sandbox can be checkpointed and resumed on another machine. H11 asked whether that is feasible at acceptable cost: migration under 2 minutes, snapshot under 1 GB. The spike measured it on Claude Code with a mock model and found it feasible; the write-up is the last comment on #24 and the evidence branch `h11-spike-evidence`. None of this is in Wave 2; fork is off in every H1 arm (D58).

**Seven decisions it asks for.** Build checkpoints now as infrastructure and gate fork-based allocation on H2 and H10, or defer; whether the 60 and 70 percent context thresholds become token counts now that windows are 1M; retention of sandbox snapshots (never deleted, about 320 GB a day at 50 workers, or deletable after N days when unreferenced); accept a pinned Claude Code version and its transcript format, or build on the Agent SDK's session store; approve block-and-snapshot for a mid-turn fork; where PoC checkpoints live and whether executors alone may write; and an API key for one real-model check.

**Recommendation.** Defer the whole set until after H1 with one line: "H11 feasible; implementation deferred past the H1 gate; revisit with H2." Close #24 as delivered. If you want the infrastructure earlier, answer items 1, 4 and 6 and open an implementation issue; the rest can follow.

---

### 1.10 Which problem is the proof of concept (open-questions row 1)

**Decided 2026-11-05 (by Claude, at your delegation):** The prompt-optimization example stands as the real problem until H1 is done, as a demonstration only. The first real project is chosen after the comparison and then takes the spec's scarcity floor.

**Why this exists.** The system is validated on testbed problems A and B. A real problem comes after (one-page §5 step 5). The question asks which real problem, with its judge latency, quota, concurrency and holdout, because that sets the eval ladder. The spec also requires that a real problem keep artificial scarcity, latency at least 10 minutes and about 2 runs per hour for the herd, even if its judge is fast, so that the mechanisms under test are exercised.

**Where you are.** The prompt-optimization example already serves as a working real-ish problem on one machine. A code-repository example with a two-level judge ladder comes with #117. You said earlier the first end-to-end on your own problem matters more than new guardrails.

**What to decide now.** Only whether the prompt example stands in as the PoC until #117 lands, or you name a repository problem now so #117's example is built around it. Latency, quota and holdout for it can be declared when it is set up.

---

## Part 2. Merges waiting on you

All are green except the one marked conflicting. Merge in the order given; the packaging three are stacked.

| PR | What it does | Why it matters | Risk |
| --- | --- | --- | --- |
| [#152](https://github.com/minyi-tang/vlah/pull/152) | Installable testbed distribution (#144) | First layer of packaging; VLAH imports `testbed.integrator` | Low; build and install tests |
| [#155](https://github.com/minyi-tang/vlah/pull/155) | VLAH installable outside the workspace (#146), stacked on #152 | The quickstart's `pip install` story; alpha exit step 1 | Low |
| [#160](https://github.com/minyi-tang/vlah/pull/160) | Guarded release automation (#147), stacked on #155 | Trusted publishing, disabled until you turn it on | Low; publishing stays off |
| [#138](https://github.com/minyi-tang/vlah/pull/138) | Operators register worker tokens on a testbed run, optionally fast-only | The herd arm's worker credentials for the slow service | Low; testbed side |
| [#141](https://github.com/minyi-tang/vlah/pull/141) | The herd arm: `vlah arm run` for `tb run --arm herd` (#122, #115) | Track B's entry point; without it no scorecard run of the herd | Medium: the paid live-worker acceptance run was to be reported separately; check the PR says whether it ran |
| [#150](https://github.com/minyi-tang/vlah/pull/150) | Autonomy-first worker mission brief (#145) | Prompt change you asked for | Conflicts with main; needs a rebase first. Prompt changes alter behavior in both arms equally, so it is safe for H1 but changes the baseline of every earlier run |
| [#31](https://github.com/minyi-tang/vlah/pull/31) | CI design proposal, `docs/ci.md` | Problem C groundwork; the "VLAH as its own CI" idea | Docs only. Merge as a proposal, or close and keep the link in #3 |

Merging #152, #155, #160 closes #144, #146, #147. Merging #141 closes #122 and #115. Merging #150 closes #145. When you merge a stacked PR with "delete branch", retarget the one above it first, or GitHub closes it.

---

## Part 3. Issues to close, with the reason to write in the closing comment

| Issue | Reason |
| --- | --- |
| #82 | Delivered: the hidden judge tier (PR #105) and the service-side fast tier with a mechanism-free worker copy (PR #116). The residual "local copy of the judge" risk is now idea 4 in #81. |
| #87 | The coding-style pass landed with the testbed branch; remaining style debt is caught by pre-commit. |
| #42 | The Wave-1 review items are on main per the issue's own last comment; the two open test gaps are tracked in #49 and #52. |
| #24 | The spike delivered its report and evidence branch; decisions deferred past H1 (section 1.9). |
| #115 | Folded into #122; closed by PR #141. |
| #10 | The testbed is in the repo and running; open requests to the testbed from herd issues should be filed one by one. |

Close when their PR merges: #144, #146, #147, #145, #122.

Relabel `later` and drop from the roadmap view, do not close: #89 (B1 isolation by filtered mirrors rather than per-worker repositories, D67), #92 (Unix sockets on Linux), #94 (CI's Linux layer 2), #97 (measurement scripts in the sandbox). These are isolation gaps you accepted for the proof of concept; they return when a real cluster does.

Keep as live work: #81, #84, #85 (testbed gate), #117, #118 (alpha exit), #124 (preregistration tooling), #132 (scale check).

Add one issue: "Quickstart runs end to end on a clean machine", the alpha exit, owning the lead-agent step (build or remove), `vlah answer`, and the `report/` files, blocked by #117 and #118.

Update #3: check the Wave 2 boxes for #119, #120, #121, #123; mark Wave L done through #131; add the alpha exit issue; link this guide.

---

## Suggested order

1. Merge #152, #155, #160, #138, #141. Rebase and merge #150.
2. Answer 1.8 (three yeses), 1.2 (herd B1 preset), 1.4 (keep A14) in `docs/open-questions.md` and `testbed/DECISIONS.md`. Ten minutes.
3. Answer 1.5 and 1.6 for the testbed-sched folder so it can be signed.
4. Write the provisional preregistration (1.1); start #124.
5. Decide 1.3's scope and acceptance number; start the #81 work.
6. Close the Part 3 issues; relabel the four; update #3.
7. Revisit 1.1's effect size after the pilot, seal, run.
