# Agent guidelines

These rules apply to every coding agent working in this repository, including Copilot, Claude Code, and Codex. `CLAUDE.md` is a symlink to this file.

## About VLAH

VLAH runs many LLM agents on one hard optimization problem, where the only trustworthy judge is slow and scarce. The bet (H1) is that agents who share as they go beat the same agents working in isolation and merged the same way, with the same time and the same judge budget.

**Where we are:** the Foundation (#4) comes first. Then 13 Wave-1 components (#11–#23) are built in parallel. The testbed (#10) is the herd's guardrail and semi-CI. The plan is the roadmap, #3.

## Start here

0. **Claim it first.** If the issue already has the `in-progress` label, someone else has it: pick another, or ask. Otherwise add the label and comment with your branch name before you write any code.
1. Find **your issue**. Its "Start here" block gives:
   - what it's blocked by;
   - your branch name;
   - the issue to close;
   - the exact tests that must pass.
2. Read the issue completely. It quotes the spec you need. For full context, see the English specs in `docs/spec/` (index: `docs/spec/README.md`; the Chinese originals are in `docs/spec/zh/`).
3. Read `docs/interfaces.md` and your package's `api.py` once they exist. They are authoritative over the issue text. In particular, the issue sketches use short parameter names; `api.py` follows [docs/coding_style.md](docs/coding_style.md) (`merged_commit`, not `mc`).

## Coding style

Before writing, modifying, or reviewing code, read and follow [docs/coding_style.md](docs/coding_style.md).

## Lint

Before every commit, run `pre-commit run --all-files` and fix every failure. [CONTRIBUTING.md](CONTRIBUTING.md#set-up-lint) explains the checks.

- Never skip or weaken a check to get it to pass: no `--no-verify`, no `SKIP=`, and no edits to `.pre-commit-config.yaml` or `scripts/check_*.py` unless that is the task.
- Fix violations of the custom checks by hand. Don't run `scripts/check_leading_underscore.py --fix` (it misses callers in other files), and use `else: pass` only when the other branch really does nothing.

## Commands

```bash
uv sync                                    # install, including dev tools
uv run pre-commit run --all-files          # lint and format: black, isort, ruff, autoflake, repo checks
uv run pyright                             # types (strict on vlah/core/types and vlah/ports)
uv run pytest                              # default selection: -m "not slow"
uv run pytest -m "l0 and <component>"      # your L0 assertions, e.g. -m "l0 and queue"
uv run pytest -m smoke                     # must stay green on every PR
uv run pytest -m slow                      # long or real-time tests, run on purpose
```

Component markers: `blackboard indexer queue eval router tools executor harness integrator budget runtime egress cli`.

## Layout and ownership

**Edit only what your issue owns.** Paths are under the package folder `vlah/` unless they say "repo root".

| Path | Owner |
|---|---|
| `core/types/`, `ports/`, `arms/config.py`, every `api.py`, `adapters/memory/`, `testing/` | **Foundation** (#4–#9): frozen contracts |
| `core/blackboard/`, `adapters/store/git/` | W1-01 #11 |
| `core/indexer/`, `adapters/store/sqlite_vec/`, `adapters/model/embed_local/` | W1-02 #12 |
| `core/queue/`, `adapters/store/redis/` | W1-03 #13 |
| `core/eval/`, `adapters/signal/testbed_http/` | W1-04 #14 |
| `core/router/` | W1-05 #15 |
| `tools/` | W1-06 #16 |
| `adapters/executor/local/` | W1-07 #17 |
| `adapters/harness/claude_code/`, `core/worker/`, `prompts/worker/` | W1-08 #18 |
| `core/integrator/` | W1-09 #19 |
| `core/budget/`, `core/stop/`, `core/metrics/` | W1-10 #20 |
| `runtime/`, `arms/` (except `config.py`) | W1-11 #21 |
| `adapters/executor/sandbox/` | W1-12 #22 |
| `cli/`, `core/problem/`, `adapters/human/cli/`; `examples/` (repo root) | W1-13 #23 |
| `spikes/checkpoint/`, `docs/spikes/h11.md` (repo root) | S-01 #24 |
| `roles/` | Wave 2 (no issues yet) |
| `testbed/`, `problems/`, `tests/testbed/` (repo root) | **testbed agents** (#10) |
| `tests/unit/<component>/` (repo root) | that component |
| `tests/l0/test_l0_<NN>.py` (repo root) | the component listed in #9's table (the Foundation creates them as stubs) |
| `tests/smoke/`, `tests/contract/`, `tests/fixtures/` (repo root) | Foundation |
| `AGENTS.md`, `CONTRIBUTING.md`, `docs/coding_style.md`, `docs/spec/`, `docs/interfaces.md`, CI, pre-commit (repo root) | maintainers (open an issue to change them) |
| `docs/DECISIONS.md` (repo root) | everyone (append rows) |

## House rules

1. **Stay in your paths.** If you need a change elsewhere, open an issue or a separate small PR.
2. **Contracts are frozen.** Every `api.py`, `vlah/core/types/`, `vlah/ports/` and `vlah/arms/config.py` change only in a **contract-only PR** labelled `contract`. If a contract doesn't fit, open that PR and wait. Never work around it.
3. **Components talk only through `api.py` and events.** Never import another component's internals. Test against `vlah.adapters.memory` and the shared fakes in `tests/fixtures/`.
4. **Inject the clock and the random generator.** Never read wall-clock time or global randomness directly. Tests must be deterministic.
5. **`[spec …]` vs `[proposed]`.**
   - `[spec]` items are fixed by the design.
   - `[proposed]` items are gap-fills in the issues. Keep or change them, but record each one in `docs/DECISIONS.md`.
   - For anything else undefined, check v2.2 §15. If it isn't there, add a DECISIONS row and list it under Open Questions in the PR. **Never decide silently.**
6. **Plain code keeps the books (D13).** The Indexer, Router, merge queue, Eval client, Budget meter and Integrator make no model calls.
7. **Tokens are not a constraint (P10).** Don't add mechanisms that save tokens, and don't use weaker models. Every role uses the same strongest model. What we ration is judge runs, wall-clock time, context windows and human attention.
8. **Done means:**
   - pre-commit, pyright, your unit tests and the smoke test pass;
   - your L0 assertions pass and are no longer `xfail`.

## Invariants that never bend

- **Append-only.** No one modifies or deletes a blackboard file, and workers write only under their own prefix (D31).
- **Merge sequence numbers only ever increase.** Only the merge queue assigns them (D56).
- **Workers never see** eval definitions, hidden data, `herd.yaml` or `private/` (D10, D63). Git has no per-path read permissions, so anything hidden stays out of the blackboard repo entirely.
- **Workers have no push credentials.** Only the tool server pushes (D68).
- **The executor sends heartbeats, not the agent** (D32, D60).
- **Only the Eval client spends judge runs, applies quota and deduplicates.** Every score carries its sample count (C3).
- **Every arm uses the same Integrator config.** Arms differ only in what workers can see (D58, D74).
- **Hidden testbed material is never committed** (#10). CI enforces this.

## The testbed boundary

- `vlah` talks to the testbed only over its HTTP API and CLI. The one exception is the shared merger, `testbed.integrator`.
- The testbed never imports `vlah`.
- Tests may import both.

## Pull requests

Before you open a pull request, read [Pull requests in CONTRIBUTING.md](CONTRIBUTING.md#pull-requests) and follow it. In short:

1. Work on a branch and never commit to `main`. Keep one change per pull request.
2. Make sure `pre-commit run --all-files` passes and that tests and docs are updated.
3. Write the title as a plain imperative sentence that says what the pull request does.
4. Fill in every required section of [the pull request template](.github/pull_request_template.md). Delete its comments and any optional section you don't use.
5. Before you submit, reread the description as a reviewer who has not seen your session. If they couldn't tell what changed and why within 30 seconds, rewrite it.
6. After creating the pull request, open it and check that the description renders: real line breaks rather than literal `\n`, and no leftover template comments.
7. When later commits change the scope, update the title and description.

For VLAH components, also:

- One component, or one contract change, per PR. Put `Closes #N` (your issue) under Related Issues.
- Use the branch name from your issue.

Agents get these wrong most often:

- **Narrating your process.** Describe the result, not the steps you took ("First I explored the repo...").
- **Pasting tool output.** Don't paste transcripts, raw logs, or full diffs. Quote only the lines that matter.
- **Unverified claims.** Name the commands you ran. Never write "all tests pass" unless you ran them, and say what you could not run.
- **Hidden decisions.** List every assumption or choice the user didn't make explicitly under Open Questions.
- **Filler.** Leave out praise, hype words, emoji headings, and closing summaries that repeat the sections above.

## Don't

- Don't build every component before running anything. Run the smoke loop, then B1, as early as possible.
- Don't start a comparison between arms without preregistration (D42, D72).
- Don't choose the PoC problem, the model id, the API limits or the preregistration numbers. Those are @minyi-tang's (see `docs/open-questions.md` once it exists).
- Don't change the testbed scorecard or its anti-cheat rules (TB§11.5).
- Don't commit secrets, judge scripts or hidden data.
