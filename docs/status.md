# Status and early findings

Last updated 2026-11-01. This page says what runs today, what the first real runs showed, and what stands between here and the H1 result. The roadmap with issue numbers is [#3](https://github.com/minyi-tang/vlah/issues/3); the questions only @minyi-tang answers are in [open-questions.md](open-questions.md).

## Where the project is

**Alpha.** The herd runs end to end on one machine, in Docker containers and in Kubernetes pods on kind, on a real problem with real LLM workers. Interfaces are still changing: the Python SDK page describes names that are not exported yet, and the quickstart's lead agent (`vlah chat`) does not exist.

| Area | State |
|---|---|
| Foundation: types, ports, in-memory adapters, test fakes | done |
| Wave 1, the 13 components (blackboard, indexer, merge queue, eval client, router, tools, local executor, worker harness, Integrator, budget and stop rules, runtime, egress sandbox, CLI) | done, 2026-10-24 |
| Wave 2, Track A: watch and steer a run (`vlah status`, `events`, `search`, `report`, `inbox`, `stop`, `demo`), durable runs with `--resume`, critic holdout checks | done |
| Wave 2, Track A: candidates as changes to a whole repository (#117), the run report files (#118) | not started |
| Wave 2, Track B: the testbed herd arm (#122, PRs open), problem A folder (#123) | in review / done |
| Wave 2, Track B: pilot and preregistration tooling (#124) | not started |
| Wave L: scripted worker, Docker executor, blackboard over HTTP, Redis store, Kubernetes executor on kind, `vlah stack` | done, 2026-10-26 |
| Wave L: scale and the H12 check across local, Docker and kind (#132) | not started |
| Testbed: problem A hard enough for the gate (#81), anti-rebuild rules (#82), tier and margin fixes (#83 to #85) | open |
| Packaging and releases (#144, #146, #147) | PRs open |

Tests, as of this commit: 754 collected, of which 30 are the L0 assertions (none `xfail` any more), 17 are `infra` tests that need Docker or kind, 4 are `slow`, 2 are the smoke tests. On every push CI runs lint, types, the default selection, the testbed self-checks, the `infra` job (Docker) and a check that git tracks no hidden testbed material; a nightly workflow runs the kind suite.

## What the first runs showed

These are observations from a handful of short runs on one toy problem. None of them is a result about H1. They are listed because they are the first evidence that the mechanisms behave as designed, and because two of them changed the code.

**The problem.** [examples/prompt_optimization](../examples/prompt_optimization/) improves the system prompt of a support assistant. A hidden set of situations is answered by a small assistant model with the candidate prompt as its only instructions, and a grader scores each reply against a public policy handbook. The starting prompt scores about 0.35; the ceiling is near 1; two runs of the same prompt differ by about 0.02. Workers are Claude Code sessions on Claude 5.5 through a LiteLLM gateway.

### 1. Sharing happens on its own, and isolation holds

Two 20-minute runs side by side, four workers each, same judge, same Integrator ([#114](https://github.com/minyi-tang/vlah/pull/114)). S2 shares the blackboard; B1 gives each worker a filtered view with only its own work.

| | S2 (shared) | B1 (isolated) |
|---|---|---|
| Deliverable score, from 0.35 | 0.86 | 0.875 |
| Worker candidates judged | 27 | 27 |
| Merges kept | 1 | 3, all from one worker |
| Tool calls reading another worker's candidate | 71 | 0 |
| Claims citing another worker's claim or candidate | 65 | 0 |

In S2 every worker built on a baseline that held another worker's merge. One worker suspected judge noise and asked for the unchanged baseline to be rerun; another ran it, measured the noise at about ±0.01 to 0.02, refuted the suspicion and posted a 0.04 threshold for a meaningful gain. In B1 no worker touched a sibling's sandbox or candidate. One pair of 20-minute runs says nothing about which arm wins; the scores are within judge noise of each other.

### 2. The same herd runs in containers with no code change to the problem

A 30-minute S2 run with two workers in Docker containers, state in Redis, the blackboard served over HTTP ([#149](https://github.com/minyi-tang/vlah/pull/149)). The run was stopped by its time limit, closed out, and exited 0.

| | |
|---|---|
| Worker sessions in containers | 33, all exited and removed |
| Judge runs | 15, 0.90 of the 20 machine-hours granted |
| Baseline@0 | about 0.34 |
| Candidates scored | 0.80 to 0.8625 |
| Merges kept | 2; the deliverable baseline@2 scored 0.8625 |
| Critic jump alerts | 3, all held on the audit set |
| Sealed holdout verdict | 0.845, the mean of 5 runs |
| Denied network connections | none; every session went through the egress proxy, which allowed only the tool server and the model gateway |

On kind, the LLM-free herd with scripted workers runs with the same event sequence and the boundary probe passes inside a pod: no `herd.yaml`, no `private/`, no push token, no service-account token, raw sockets and off-list names blocked ([#158](https://github.com/minyi-tang/vlah/pull/158)). A real run with LLM workers on kind has not been done yet.

### 3. A void measurement must never revert a merge

In the first Claude Code run the grader ran out of tokens before printing its JSON on the closing audit. The core read the empty scores as "no audit gain" and reverted a merge that had taken the prompt from 0.39 to 0.83. The rule is now that a void remeasure or audit is resubmitted twice and, if the judge still cannot score it, skipped with an error rather than treated as a result ([#114](https://github.com/minyi-tang/vlah/pull/114), recorded in [DECISIONS.md](DECISIONS.md)).

### 4. The spot-check threshold is too tight for a model-graded judge

In the shared run the spot check reverted the best prompt (0.90): its gain on the daily set was 0.55 and on the audit set 0.41, a gap of 0.14 against a threshold of 0.12. The threshold comes from the standard error of baseline@0's per-situation scores, which understates how much a gain differs between two sets of situations. Calibrating it from paired gains is an open follow-up.

### 5. Most of what broke was plumbing, not the design

The first three real runs each failed for a reason the unit tests could not see: the sandbox root sat inside the run directory, the sandbox profile denied the session's own log files, and a worker submitted its whole clone with `.git` as the candidate and saw an opaque error ([#109](https://github.com/minyi-tang/vlah/pull/109)). Each took one run to find and one fix. The lesson we act on: run the real loop early and often, and treat every new executor as unproven until an LLM worker has gone through it.

## What "passed pre-alpha" meant, and what alpha means here

Pre-alpha meant the design was written and nothing ran. That stopped being true on 2026-10-24. We call the current state alpha, which means:

- a maintainer can run the herd on a real problem, on one machine or on the local stack, and get a deliverable and a report;
- the mechanisms the design relies on (append-only blackboard, one merge queue, a single judge client with quota, filtered views for isolation, egress enforcement) are built and have L0 assertions;
- interfaces may still change, and a stranger cannot yet follow the quickstart to the end.

## What comes next

In order:

1. **Make the quickstart true.** Merge the packaging PRs, land whole-repository candidates (#117) and the report files (#118), and either build or remove the lead agent step. Done when a clean machine can follow [quickstart.md](quickstart.md) as written.
2. **Make the testbed gate real.** Problem A hard enough that one agent cannot reach the target in 30 minutes (#81), then the 5-worker B1 smoke through the scorecard.
3. **Preregister and run H1.** Needs the numbers in [open-questions.md](open-questions.md), the pilot tooling (#124), and the preregistration document. The comparison is S2 against B1 plus the same merger, at 5 workers, on problem A. We publish the result whichever way it comes out.
