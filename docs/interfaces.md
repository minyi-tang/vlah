# Interfaces

This page maps every contract in the herd to the code that defines it. **The code is authoritative.** If this page and a module disagree, the module wins; fix the page in the same PR.

All contracts are frozen. They change only in a contract-only PR labelled `contract` (see [AGENTS.md](../AGENTS.md)). Gaps the spec left open, and how they were filled, are in [DECISIONS.md](DECISIONS.md).

## Where each contract lives

| Layer | What | Module |
|---|---|---|
| Core types | Actor ids, roles, write priority (D34) | `vlah/core/types/ids.py` |
| | Clock: injected, never the wall clock | `vlah/core/types/clock.py` |
| | Claims and their files | `vlah/core/types/claims.py` |
| | Status machines per claim type and for approaches (D9) | `vlah/core/types/states.py` |
| | Events and their payloads (D56, D74, D76) | `vlah/core/types/events.py` |
| | Tasks and leases (D55, D32) | `vlah/core/types/tasks.py` |
| | Interrupts (section 8) | `vlah/core/types/interrupts.py` |
| | Candidate manifest and hash (D69, D73, TB§11.2) | `vlah/core/types/candidates.py` |
| | Judge declarations, scores, paired results (D39, D73) | `vlah/core/types/sources.py` |
| | Baseline records (D74) | `vlah/core/types/baseline.py` |
| | Charter files code reads: params, gates, egress, limits, target | `vlah/core/types/params.py` |
| | Blackboard write rules | `vlah/core/types/paths.py` |
| | Token count used for every budget | `vlah/core/types/tokens.py` |
| Arms | The D58 switch table and presets B0, B1, S1, S2, S2P | `vlah/arms/config.py` |
| Ports | Executor, Harness, SignalSource, Store, Human, Model; shared errors | `vlah/ports/` |
| In-memory adapters | One per port, plus the scripted executor and harness and the fake judge | `vlah/adapters/memory/` |
| Conformance | One check per port, which real adapters must pass | `vlah/testing/conformance.py` |
| Components | One frozen `api.py` each (owners below) | `vlah/**/api.py` |
| Test wiring | A herd stepped on a fake clock, for tests and the demo | `vlah/runtime/wiring.py` |
| Test fakes | Scaffolding fakes of the component contracts | `tests/fixtures/fakes.py` |

| Component contract | Owner |
|---|---|
| `vlah/core/blackboard/api.py` | W1-01 #11 |
| `vlah/core/indexer/api.py` | W1-02 #12 |
| `vlah/core/queue/api.py` | W1-03 #13 |
| `vlah/core/eval/api.py` | W1-04 #14 |
| `vlah/core/router/api.py` | W1-05 #15 |
| `vlah/tools/api.py` | W1-06 #16 |
| `vlah/ports/executor.py` (the local executor implements it) | W1-07 #17 |
| `vlah/ports/harness.py` (the Claude Code harness implements it) | W1-08 #18 |
| `vlah/core/integrator/api.py` | W1-09 #19 |
| `vlah/core/budget/api.py`, `vlah/core/stop/api.py`, `vlah/core/metrics/api.py` | W1-10 #20 |
| `vlah/runtime/api.py` | W1-11 #21 |
| `vlah/adapters/executor/sandbox/api.py` | W1-12 #22 |
| `vlah/core/problem/api.py` | W1-13 #23 |

## What the worker loop asks its adapters

These methods exist so that the loop in `vlah/core/worker/loop.py` can run on the ports alone (D61: a capability the loop needs is a port field, never a protocol of its own):

| Method | Port | What it answers |
|---|---|---|
| `recent_tool_calls(transcript_path, count)` | `Harness` | the last `count` tool calls, one line each, for the D77 restart pack |
| `rate_limit_retries(transcript_path)` | `Harness` | how often the model API throttled the session (C7) |
| `sandbox_path(agent, task_id)` | `Executor` | where the task's sandbox is; the loop registers it with the tool server |
| `transcript_path(handle)` | `Executor` | where the session's current process writes the transcript the harness reads; still answered after the session ends |
| `base_revision(agent, task_id)` | `Executor` | what `target/` started from, for the working-directory diff (D77) |
| `register_agent(agent, sandbox_root, task_id)` | `ToolServer` | the agent's bearer token; `eval_request` paths resolve in that sandbox and earlier claims count for that task |

## Files people write

### charter/params.yaml

Keys and defaults are the fields of `Params` in `vlah/core/types/params.py`. Every default is the spec's default and is written only there. Every arm of a comparison shares one `Params`, so the Integrator settings (`accept_*`, `interaction_*`, `spot_check_*`, `guard_every_merges`, `quota_integrator_*`, `holdout_run_cap`) can't differ between arms.

### charter/gates.yaml

A list of gates. A gate passes or fails, and never becomes a score (D54):

```yaml
- name: unit_tests
  command: pytest -q tests/
  when: [pre_submit, post_result]   # the eval client runs these
- name: latency_budget
  command: private/check_latency.sh
  when: [guard]                     # the Integrator batches these every guard_every_merges merges
```

### charter/egress.yaml

The machine-readable allowlist that sits alongside `egress.md` (D75). The herd adds its own hosts (the tool server, the slow-eval host, the model API host):

```yaml
allow:
  - {host: pypi.org, ports: [443], purpose: mirror}
  - {host: "*.example-docs.com", ports: [443], purpose: context}
```

### Limits and target

`charter/constraints.md` must contain a limits block; a folder without one is rejected:

````markdown
```yaml limits
eval_machine_hours: 96
days: 2
runs: {bench_full: 96}
```
````

`charter/progress.md` may contain a target block:

````markdown
```yaml target
source: bench_full
score: 70
direction: higher
```
````

### eval/sources/<id>.yaml

Seven fields are required: `id`, `mode`, `latency`, `budget`, `cost_machine_hours`, `trust` and `readable_by`. See `SourceDecl` in `vlah/core/types/sources.py`. `latency` is written as `90s`, `2m`, `6h` or `1d`.

### herd.yaml

Workers never see this file. It parses into `HerdConfig` in `vlah/core/problem/api.py`: `judges`, `executor`, `harness`, `stores`, `models` and `human`.

### Router rules

`vlah/core/router/rules.default.yaml` lists the router's rules (v2.2 section 8). A rule changes by editing this file, without a code change. Each entry has these fields:

| Field | Meaning |
|---|---|
| `id` | unique name, used to deduplicate interrupts |
| `rule` | the rule number from v2.2 section 8, reported on each interrupt |
| `events` | event types that trigger it, or `claim` for a newly merged claim (not `on`, which YAML reads as true) |
| `match` | one lease predicate from the fixed set in `vlah/core/router/rules.py`; a file naming another fails to load |
| `kind` | `hard` blocks the agent's next tool call; `soft` waits for its next sync point |
| `priority` | among pending hard interrupts, the lowest number is shown and the rest are counted |
| `enabled_by` | the `ArmConfig` flag that turns the rule on: `router_rule1`, `router_rules_2_5` or `router_rule6_baseline_push` |
| `reason` | the text the agent sees; a template whose fields are listed in `rules.py` |

Rule 7 ("everything else waits for the diff") is the absence of a matching rule.

## Blackboard

### Layout and who may create files

Nobody modifies or deletes an existing file (D31). The rules below decide only who may create a path; the first matching rule wins (`WRITE_RULES` in `vlah/core/types/paths.py`).

| Path | Who may create it |
|---|---|
| `events/_seq/<n>` | merge queue |
| `claims/<a>/`, `events/<a>/`, `traces/<a>/`, `checkpoints/<a>/` | actor `a` itself |
| `candidates/<h>/result.json`, `lineage.json` | eval service |
| `candidates/<h>/manifest.yaml`, `prediction.json`, `local.json`, `candidate*` | workers, humans |
| `digests/` | Librarian |
| `decisions/` | Planner |
| `allocation/` | Planner, the allocation rule |
| `baseline/<k>.json`, `report/`, `map/gap.md`, `map/measurements/<id>/<k>/` | Integrator |
| `map/measurements/<id>/<script>` | workers (S2 only, enforced by arm), humans |
| `charter/`, `spec.md`, `approaches/`, `eval/sources/` | humans, the PI agent |

### Sequence numbers

The versioned store assigns them at merge time:
- every new `events/<actor>/<name>.json` gets the next number, in path order within a commit;
- `events/_seq/<n>` holds the event's path;
- event files themselves keep `seq: null`;
- reading at a sequence number gives the state right after the merge that assigned it.

### Candidate hash

The hash is SHA-256, written as lowercase hex, over:
1. for each payload file, in sorted POSIX relative-path order: the UTF-8 path, one NUL byte, then the raw bytes, with nothing between files;
2. then the UTF-8 target baseline id.

Excluded files:
- any `__pycache__` directory, at any depth;
- any `*.pyc` file, at any depth;
- the top-level `manifest.yaml` (a `manifest.yaml` in a subdirectory is part of the payload).

The layout and a test vector are posted on #10. `tests/contract/test_candidate_hash.py` compares the herd's hash with the testbed's.

## Test gates

| Gate | What | Command |
|---|---|---|
| Smoke | Two scripted workers, one judge run each, results returned to their submitters | `uv run pytest -m smoke` |
| L0 | The 20 plumbing assertions of v2.2 §14.1, with 5 and 18 split (22 tests); each file names its owner | `uv run pytest -m "l0 and <component>"` |
| Contract | The candidate hash against the testbed's | `uv run pytest -m contract` |
| Conformance | Each real adapter against its port's check | in each adapter's unit tests |

## Conventions

- **Tags in issues:**
  - `[spec …]` items are fixed by the design;
  - `[proposed]` items are gap-fills, recorded in `DECISIONS.md` when they are implemented.
- **Time is epoch seconds** from an injected `Clock`. Randomness is a seeded `random.Random` passed in by the runtime.
- **Names follow [coding_style.md](coding_style.md).** Wire-schema names from the spec stay as they are (`seq`, `ts`, `n_items`, `n_reps`, `from`).
