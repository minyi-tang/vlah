# CI design

> **Status: proposal.** Nothing here changes CI until @minyi-tang approves it. Each phase in [Rollout](#rollout) lands as its own pull request, and the choices this page would make are listed under [Open questions](#open-questions).

This page designs the continuous integration (CI) for this repository and answers one question: can VLAH be the CI for VLAH?

**Short answer: yes, in two steps.**

1. **Now: build the CI as a VLAH evaluation ladder.** This repository's most trusted check is a run of the herd on a testbed problem with real LLM workers. That check is slow, scarce and noisy, and it costs money, which is exactly the kind of judge VLAH is built around. So the CI should ration it the way the Eval client rations a judge: cheap checks first, never paying twice for the same comparison, paired comparisons against `main`, and a sample count on every score.
2. **After H1: problem C.** The herd proposes changes to `vlah/`, and this same ladder is its judge. The one-page alignment (§4) already plans this. It is safe only under the [bootstrapping rules](#problem-c-the-herd-improves-vlah) below, and those rules also constrain what we build in step 1.

In v2.2, "CI" also names the blackboard's own synchronous checks (D31, D33, D68). This page is about the GitHub Actions CI of this repository. The two do the same job, as the next table shows.

## Why this is a VLAH problem

VLAH targets work where many agents change one codebase and the trusted judge is slow and rationed. Building VLAH is such work. The 13 Wave-1 agents change one repository in parallel. The scarcest checks are an L1 testbed run, which takes hours of LLM workers, and the maintainer's review, which gets about 30 minutes a day. Each VLAH mechanism therefore has a direct CI counterpart:

| VLAH mechanism | CI counterpart |
|---|---|
| Candidate | The head of a pull request |
| Moving baseline `baseline@k` (D74) | `main` |
| Ladder of signal sources, cheapest first (D39) | The rungs R0–R5 below; a rung runs only if the cheaper ones passed |
| Hash dedup, with the baseline in the hash (D69, D73) | A pull request whose herd code equals `main`'s needs no L1 comparison, and a finished comparison is reused until `main`'s herd code changes |
| Paired measurement against the current baseline (D73) | The pull request and `main` run side by side in the same batch |
| Every score carries its sample count (C3) | Every L1 result states its number of batches |
| Quota | A daily cap on L1 runs |
| Gates (D54) | Required checks |
| Remeasure on the current baseline before merging (D74) | R0–R3 rerun on an up-to-date branch; an L1 result goes stale when `main`'s herd code changes |
| Writers stay under their own prefix; the CI path check is a second line of defense (D31, D68) | Each pull request touches only the paths its issue owns |
| Workers never see eval definitions or hidden data (D10, D63) | Coding agents never see the CI's hidden seeds, per-seed results or the reference score |
| Human attention is about 30 minutes a day (§10) | One summary comment per pull request |

## What is missing today

The current workflows run pre-commit, pyright, pytest on Python 3.11 and 3.13, a placeholder for the testbed self-checks, and the hidden-material guard. That covers the cheap rungs. The gaps:

1. **A red check does not block a merge.** The repository is private and owned by a user account on GitHub Free. Branch protection and rulesets are not available on that plan; the API answers 403, "Upgrade to GitHub Pro or make this repository public". A merge queue needs a repository owned by an organization. CONTRIBUTING.md says a failing pull request is not merged, but nothing enforces it.
2. **The house rules are prose.** Path ownership, contract-only pull requests, imports only through `api.py`, the injected clock and random generator, and "no model calls in the bookkeeping components" (D13) are checked only by reviewers.
3. **One pytest step hides which gate broke.** A smoke failure and a unit failure look the same.
4. **There is no L1 tier yet.** This is expected: it needs the testbed (#10) and Wave 1.
5. **Hardening.** `ci.yml` has no `permissions:` block and no timeouts, `actions/checkout` leaves its token on disk, and third-party actions are pinned by tag rather than by commit. All of this matters once L1 jobs run near secrets.
6. **Minutes.** GitHub rounds each job up to a whole minute, so one push costs at least 6 billed minutes today. This plan includes 2,000 minutes a month; October so far used 26.

## The ladder

| Rung | Runs on | What | Time | LLM | Blocks merge |
|---|---|---|---|---|---|
| R0 Static | every push | pre-commit, pyright, the hidden-material guard, the [boundary checks](#boundary-checks) | < 2 min | no | yes |
| R1 Plumbing | every push | Unit tests, the L0 tests, the smoke loop and the contract tests, each as its own step | < 2 min | no | yes |
| R2 Testbed self-check | every push | TB§11.9: `perfect_herd`, `lone_wolf` and `quota_hog` on `problems/stub` with the compressed knobs, plus the other §11.9 tests | minutes | no | yes |
| R3 Wiring `[proposed]` | every push, once Wave 1 lands | Scripted herd workers run through the real adapters (git store, KV, local executor, tool server) against the real slow tier over HTTP, on `problems/stub`. Workers scripted like `perfect_herd` must get an all-green scorecard, and workers scripted like `lone_wolf` must get its red rows | minutes | no | yes |
| R4 L1-short | nightly on `main`; on a pull request by label | S2 on problem A with the R4 knobs (open question 3), paired with `main` | hours | yes | no; a tripwire |
| R5 L1-full | by hand, for L1 acceptance or before a release | The §14.1 L1 acceptance run: the testbed's arms at the spec's knobs (TB§8, TB§11.3), scored by the scorecard. A diagnostic, never an H1 comparison (D42, D72) | days | yes | report only |

Rules for every rung:

- **Cheap before expensive.** R4 and R5 spend nothing unless R0–R3 are green on the same commit.
- **R0–R3 are deterministic.** No retries. A flaky test is a bug in the test or a missing injected clock (house rule 4). It gets fixed, not rerun.
- **R4 and R5 are stochastic.** They always report the number of batches and an interval, never a bare number (C3).
- **Steps, not jobs.** R1's parts run as separate steps of one job, each with `if: ${{ !cancelled() }}`, so every failure shows on its own without adding billed minutes.

R3 is the one new tier. It is the herd-side mirror of TB§11.9. R2 checks that the scorecard reads the recorder correctly. R3 checks that the herd writes to the recorder what the scorecard expects. A broken event, such as an `allocation` without a hash, then shows up in minutes rather than as a red "tilt" row at the end of a two-day L1 run. R3 belongs to W1-11 (#21) or to the end-to-end item of Wave 2.

## Boundary checks

These checks turn the house rules in AGENTS.md into R0. They follow P11: enforce the boundaries, and leave the paths to the agents.

| Rule | Check |
|---|---|
| Stay in your paths | Map each changed file to its owner with a machine-readable owners file, which CI checks against the table in AGENTS.md so the two can't drift. The file includes the shared paths (`docs/DECISIONS.md`) and the L0 file owners from #9. Fail if a pull request touches the paths of more than one owner, or touches maintainer paths, unless its description lists each extra path under an explicit override |
| Contracts are frozen | A pull request that touches an `api.py`, `vlah/core/types/`, `vlah/ports/` or `vlah/arms/config.py` must have the `contract` label and touch nothing else |
| Components talk only through `api.py` | Outside `vlah/core/<x>/`, an import from `vlah.core.<x>` may name only `vlah.core.<x>.api` |
| The testbed boundary | `testbed` never imports `vlah`; `vlah` imports only `testbed.integrator`; `vlah.core` never imports `vlah.adapters` |
| Plain code keeps the books (D13) | The Indexer, Router, merge queue, Eval client, Budget meter and Integrator import neither the Model port nor any model SDK |
| Inject the clock and the random generator | No direct `time.time`, `datetime.now`, module-level `random` functions, `uuid.uuid4` or `os.urandom` in `vlah/`, outside an allowlist (the real clock and the runtime that seeds the generator). `run_demo()` gives the same event log twice for one seed (#21) |
| L0 assertions only move forward | F5's skeletons end in a failing placeholder, and the L0 step runs with strict `xfail`. A test that starts passing then fails CI until its owner removes the marker. Separately, the number of L0 tests marked `xfail` never goes up. This changes #9, which uses `strict=False` because an empty skeleton passes |
| The testbed stays a fair test (D10; docs/spec/README.md) | `vlah/prompts/` and `vlah/roles/` do not name the testbed's planted ingredients, trap or bugs. The agents writing the prompts have read `herd-testbed.md`, and a prompt that hints at the answers makes an L1 pass meaningless |

The import rules can be `import-linter` contracts or a small script in the style of `scripts/check_*.py`.

Every agent currently pushes with the maintainer's credentials, so a label, an override or a "Closes #N" line is a declaration, not proof of who wrote the change. These checks catch accidents, not attacks. Anything that must hold against hostile code relies on the isolation described in the next section.

## L1 in CI

R4 is where CI starts spending a scarce judge, so it follows the Eval client's rules.

### When it runs

- Nightly on `main`, as an A/A pair: two copies of the same commit run at the same time. Their difference measures the noise that R4 needs. This is not the D72 pilot, which runs B1+M and S2 together.
- On a pull request with the `run-l1` label, once R0–R3 are green.
- Never paying twice for the same comparison. A comparison is identified by a run manifest: the hash of each side's herd code (`vlah/`, `uv.lock`, `pyproject.toml`, and the herd's problem folder for A, planned in #23), and the judge and environment both sides share (the hashes of `testbed/` and `problems/`, the private config, the model id, the harness version, the container image and the knobs).
  - A pull request whose herd code equals `main`'s, for example one that changes only docs or tests, needs no comparison.
  - A comparison that already has its K batches is reused, until `main`'s herd code changes and the result goes stale.
  - Each batch has its own replicate id. A cached result never counts as a second batch.
- At most the daily cap. Extra requests wait, first in, first out.

### How it decides

- The pull request and `main` run as K paired batches. Within a batch both run at the same time, with separate run ids, so drift on the model provider's side cancels out (as in D72).
- The number compared is the raw score of each final deliverable on the CI seeds. Per batch, d = pull request minus `main`.
- δ is the smallest drop that matters. As with D72's minimum meaningful effect, a human sets it.
- R4 flags a regression when a one-sided paired t-test at α = 0.05 rejects "the pull request is at most δ worse than `main`".
- K is chosen from the A/A noise for the power the maintainer wants.
- Not flagging is not evidence that a pull request is fine. R4 is a tripwire for large regressions, and small effects are H1's job, with preregistration. Making R4 block merges would need a non-inferiority test, with the null hypothesis reversed, and many more batches.
- The behavior rows of the scorecard are shown next to `main`'s, as description only. They are not a gate: R4's shortened runs have not been checked against full-length runs, and no rule for comparing rows has been set.

R4 compresses wall-clock time, but the slow tier must stay scarce relative to the workers' pace: a few submissions per worker, not 100 an hour as in the self-check knobs. Without scarcity the run no longer tests what the herd is for (C1). §14.1 keeps artificial scarcity even for real problems.

### Data hygiene

- R4 and R5 use their own hidden seed set and never touch the seeds or holdouts reserved for the H1 verdict (D66, D72). Every CI result that a person or an agent reads leaks a little about the seeds behind it. After hundreds of pull requests, a seed set shared with the verdict would be overfit.
- CI publishes the paired difference with its interval and K, and the colors of the behavior rows except "share of available gain captured". That row and the calibration event stay private, along with recorder files and per-seed scores. Together with the baseline from `/meta` and a final score, the share would reveal the reference score, which the slow tier never returns (TB§11.3).
- The testbed needs a separate private problem config with the CI seeds, to be requested on #10.

### Isolation

The testbed says it is "not an anti-cheating security system" (TB§0), and its three anti-cheat rules (TB§2) assume cooperative agents. R4 runs LLM-written code with a model credential, and in problem C that code is optimized against the judge. So R4 needs its own boundary:

- The seeds and the scoring code stay on the slow-eval machine. The job reaches them only through the slow tier's HTTP API. They never go into GitHub secrets or the job's environment, where the code under test could read them.
- The whole herd arm, not only its workers, runs inside an outer sandbox that CI owns: a fresh VM or unprivileged container per job, with no Docker socket, host mounts or cloud metadata, and egress only to the slow tier and a model-API proxy. The herd's own egress allowlist (D75) runs inside it, so a candidate that breaks its own sandbox still can't get out.
- The proxy issues a short-lived model credential scoped to the run, with a spending cap.
- The workflow token is read-only, `actions/checkout` runs with `persist-credentials: false`, and code from a pull request never runs under `pull_request_target`.

H1 runs use one frozen build (D72), pinned to a tag, and nothing CI measures feeds the verdict.

## Problem C: the herd improves vlah

The one-page alignment (§4) defines it: "The herd itself is a problem pack (problem C): candidate = a change to the herd code; fast tier = unit tests and scripted self-tests; slow tier = a testbed run on problems A and B using that version of the herd. The judge is out of its hands. A human takes a look before anything merges into trunk."

With the ladder above, problem C needs no new judge: R0–R3 are its fast tier, and R4–R5 are its slow tier. Its problem folder declares each rung as an eval source (D39) in `eval/sources/`, the format `SourceDecl` reads:

```yaml
# eval/sources/r4_l1_short.yaml in problem C's folder; values in <> are the maintainer's
id: r4_l1_short
mode: pull
latency: <set by the R4 knobs>
budget: {per_day: <daily cap>, allocated_by: human}
cost_machine_hours: <measured>
trust: <maintainer>
readable_by: <maintainer>
noise: {n_for_ci: <K>}
```

Problem C also needs the following rules, because the thing being optimized is the thing doing the optimizing.

1. **A frozen floor, outside the candidate.** The trusted side is the CI workflows, the outer sandbox and the model-API proxy, the testbed (runner, slow tier, recorder, scorecard), and a released, tagged `vlah` that runs problem C's own workers, Eval client and Integrator. A candidate runs only as the arm under test, inside the outer sandbox. v2.2 §9 says the same for any harness problem: "otherwise the herd would be changing the floor under its own feet." Compilers bootstrap the same way: the released compiler builds the new one, and the new one never judges itself.
2. **The judge is out of the candidate's reach.** Candidates may change `vlah/`, at first without the frozen contracts. A change to the candidate's own runtime, Eval client or sandbox adapter affects only the arm under test, which the outer sandbox contains. Candidates may not change `testbed/`, `problems/`, `tests/testbed/`, `.github/`, `scripts/` or `.pre-commit-config.yaml`.
3. **Workers never see the answers.** The problem pack is a history-free export of allowlisted paths, with no `.git`, remote or cache, and the workers' egress allowlist excludes the repository host. Otherwise `git show` or a fetch would recover everything. The pack leaves out `docs/spec/` (including `zh/`), `testbed/`, `problems/` and `tests/testbed/`, because `herd-testbed.md` describes the planted ingredients, trap and bugs ("Caution for later work" in docs/spec/README.md).
4. **The gate reads only trusted records.** In the recorder (TB§11.4), the slow tier writes `calibration`, `submit`, `result` and `final`, and the testbed's arm runner writes `final_check`. The herd under test writes `blackboard_write`, `blackboard_read`, `fork`, `allocation` and `abandon`, and its workers self-report `fast_run`. A candidate herd could emit events that make breadth, tilt or conflict handling look good, so in problem C those rows are diagnostics. The gate is the slow tier's score for the final deliverable and the ingredients its probes find.
   - `/events` accepts any event type today (TB§11.3), so a candidate could also post a fake `result`. The slow tier must set each event's origin itself and refuse its own and the runner's event types on `/events`.
   - TB§11.10 rules out authentication in the testbed, so how the runner proves its origin needs a decision on #10 before problem C starts.
5. **Judge on problems the herd has not trained on.** Optimized against fixed problems A and B, the testbed becomes a training benchmark, which TB§0 says it is not. The herd would learn problems A and B rather than how to work as a herd, for example by writing scheduling hints into its prompts.
   - Problem C's slow tier should use freshly generated hidden variants of A and B.
   - A sealed promotion set decides what reaches the maintainer, and its results never go back to the herd.
   - A private scan of diffs for the names of planted ingredients is only a backstop: it can't catch hard-coding that doesn't use the names.
6. **A human merges.** The herd's Integrator stacks verified wins on its own baseline branch and opens a pull request. R0–R3 run again on it, and the maintainer reviews it. Making a merged change the new frozen floor is a separate step: tagging a release.
7. **Not during H1.** Problem C starts after the H1 verdict, so the herd that H1 measures does not change during the experiment (one-page §5, step 6).

Rules 1–4 shape what we build now. R4 must be able to run a pinned `vlah` as the orchestrator and any commit as the arm, keep the seeds out of the job, own the outer sandbox, and take its gate from records whose origin the slow tier set.

## Rollout

| Phase | Starts when | Delivers |
|---|---|---|
| P0 | now | Enforcement (open question 1); read-only `permissions:`, `persist-credentials: false`, timeouts, and actions pinned by commit; R1 split into steps; the boundary checks; failing L0 placeholders, which are easiest to land before F5 merges (#9) |
| P1 | the testbed lands (#10) | R2 filled in; the candidate-hash contract test active; the requests on #10 for a CI seed set and for event origin |
| P2 | Wave 1 lands | R3 wiring |
| P3 | the herd runs on problem A | R4 nightly and by label; the outer sandbox and the model-API proxy; a ledger of results |
| P4 | the H1 verdict is in | Problem C: the history-free problem pack with its `eval/sources/`, the frozen-floor runner, fresh problem variants and a sealed promotion set |

Each phase is its own pull request. The choices it makes go into `docs/DECISIONS.md` when it lands.

## Open questions

These are @minyi-tang's calls. Agents must not guess the answers.

1. **Enforcement.** How should red checks block merges?
   - (a) GitHub Pro keeps the repository private and adds branch protection with required checks and "require up to date". Recommended.
   - (b) Making the repository public adds branch protection for free.
   - (c) Moving the repository to an organization. A merge queue on a private repository also needs GitHub Enterprise Cloud.

   "Require up to date" makes each pull request rerun R0–R3 after every merge. That is cheap while those rungs take a few minutes.
2. **Where the herd arm runs for R4.** An ephemeral self-hosted runner on a machine separate from the slow-eval machine, or GitHub-hosted runners. Either way the seeds stay on the slow-eval machine. Recommended: self-hosted, because R4 jobs last hours and the plan's 2,000 minutes a month come to about 33 hours.
3. **The L1 budget and margin.** The daily cap on R4 runs, the spending cap of the model-API proxy, the R4 knobs (hours, workers, quota), and δ, the smallest drop that matters. CI must not choose these.
4. **Whether R4 should ever block merges.** That needs a non-inferiority test and many more batches. Proposed: R4 stays a tripwire.
5. **Failing L0 placeholders.** Should F5 (#9) switch from empty skeletons with `strict=False` to failing placeholders with strict `xfail`? This is easiest before F5 merges.
6. **Labels and overrides.** Whether to add `run-l1` next to the existing `contract`, and whether an override listed in the pull request description is the right form for touching another owner's paths.
7. **The scope of problem C.** Whether it stays step 6 (roadmap #3, open question 3); whether candidates may ever propose contract changes; the `trust` and `readable_by` of its sources (Appendix C); and how the testbed's runner proves the origin of its events without authentication (TB§11.10).
