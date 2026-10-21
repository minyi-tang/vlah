# VLAH: working agreement for every agent

VLAH runs many LLM agents on one hard optimization problem, where the only trustworthy judge is slow and scarce. The bet (H1) is that agents who share as they go beat the same agents working in isolation and merged the same way, with the same time and the same judge budget.

**Where we are:** the Foundation (#4) comes first. Then 13 Wave-1 components (#11–#23) are built in parallel. The testbed (#10) is the herd's guardrail and semi-CI. The plan is the roadmap, #3.

## Start here

1. Find **your issue**. Its "Start here" block gives:
   - what it's blocked by;
   - your branch name;
   - your PR title (`[W1-NN] …`), with a PR body that starts with `Closes #N`;
   - the exact tests that must pass.
2. Read the issue completely. It quotes the spec you need. For full context, see `docs/spec/` (the index is `docs/spec/README.md`).
3. Read `docs/interfaces.md` and your package's `api.py` once they exist. They are authoritative over the issue text.

## Commands

```bash
uv sync                                    # install
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest                              # default: -m "not slow"
uv run pytest -m "l0 and <component>"      # your L0 assertions, e.g. -m "l0 and queue"
uv run pytest -m smoke                     # must stay green on every PR
uv run pytest -m slow                      # long / real-time tests, run on purpose
```

Component markers: `blackboard indexer queue eval router tools executor harness integrator budget runtime egress cli`.

## Layout and ownership

**Edit only what your issue owns.** Everything below is under `src/vlah/` unless it says otherwise.

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
| `cli/`, `core/problem/`, `adapters/human/cli/`, `examples/` (repo root) | W1-13 #23 |
| `spikes/checkpoint/`, `docs/spikes/h11.md` (repo root) | S-01 #24 |
| `roles/` | Wave 2 (no issues yet) |
| `testbed/`, `problems/`, `tests/testbed/` (repo root) | **testbed agents** (#10) |
| `tests/unit/<component>/` | that component |
| `tests/l0/test_l0_<NN>.py` | the component listed in #9's table (the Foundation creates them as stubs) |
| `tests/smoke/`, `tests/contract/` | Foundation |
| `CLAUDE.md`, `AGENTS.md`, `docs/spec/`, `docs/interfaces.md`, CI | Foundation (open an issue to change them) |
| `docs/DECISIONS.md` | everyone (append rows) |

## House rules

1. **Stay in your paths.** If you need a change elsewhere, open an issue or a separate small PR.
2. **Contracts are frozen.** Every `api.py`, `vlah/core/types/`, `vlah/ports/` and `vlah/arms/config.py` change only in a **contract-only PR** labelled `contract`. If a contract doesn't fit, open that PR and wait. Never work around it.
3. **Components talk only through `api.py` and events.** Never import another component's internals. Test against `vlah.testing.fakes` and `vlah.adapters.memory`.
4. **Inject `Clock` and `Rng`.** Never read wall-clock time or global randomness directly. Tests must be deterministic.
5. **`[spec …]` vs `[proposed]`.**
   - `[spec]` items are fixed by the design.
   - `[proposed]` items are gap-fills in the issues. Keep or change them, but record each one in `docs/DECISIONS.md`.
   - For anything else undefined, check v2.2 §15. If it isn't there, add a DECISIONS row and mention it in the PR. **Never decide silently.**
6. **Plain code keeps the books (D13).** The Indexer, Router, merge queue, Eval client, Budget meter and Integrator make no model calls.
7. **Tokens are not a constraint (P10).** Don't add mechanisms that save tokens, and don't use weaker models. Every role uses the same strongest model. What we ration is judge runs, wall-clock time, context windows and human attention.
8. **Done means:**
   - your L0 assertions pass and are no longer `xfail`;
   - the smoke test passes;
   - your unit tests pass;
   - ruff and pyright are clean;
   - the PR uses the template.

## Invariants that never bend

- **Append-only.** No one modifies or deletes a blackboard file, and workers write only under their own prefix (D31).
- **Merge seq only ever increases.** Only the merge queue assigns it (D56).
- **Workers never see** eval definitions, hidden data, `herd.yaml` or `private/` (D10, D63). Git has no per-path read permissions, so anything hidden stays out of the blackboard repo entirely.
- **Workers have no push credentials.** Only the tool server pushes (D68).
- **The executor sends heartbeats, not the agent** (D32, D60).
- **Only the Eval client spends judge runs, applies quota and dedups.** Every score carries `n` (C3).
- **Every arm uses the same Integrator config.** Arms differ only in what workers can see (D58, D74).
- **Hidden testbed material is never committed** (#10). CI enforces this.

## The testbed boundary

- `src/vlah` talks to the testbed only over its HTTP API and CLI. The one exception is the shared merger, `testbed.integrator`.
- The testbed never imports `vlah`.
- Tests may import both.

## Don't

- Don't build every component before running anything. Run the smoke loop, then B1, as early as possible.
- Don't start a comparison between arms without preregistration (D42, D72).
- Don't choose the PoC problem, the model id, the API limits or the preregistration numbers. Those are @minyi-tang's (see `docs/open-questions.md` once it exists).
- Don't change the testbed scorecard or its anti-cheat rules (TB§11.5).
- Don't commit secrets, judge scripts or hidden data.

## Git

- Branch names, PR titles and PR bodies (`Closes #N`) follow your issue.
- Small PRs: one component, or one contract change, per PR.
- End commit messages with the attribution line your environment specifies.
