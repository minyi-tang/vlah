# Decision log

Record every implementation decision the spec leaves open here: every `[proposed]` item from an issue that you kept or changed, and every gap you filled. Never decide silently (v2.2 Appendix C).

Before adding a row, check v2.2 §15 (`docs/spec/research-herd-design-v2.2.md`): the answer may already be there. The testbed keeps its own log in `testbed/DECISIONS.md`.

**Expensive to change?** means: would changing this later force a contract PR, a data migration, or invalidate runs that already happened?

| D# or topic | Decision | Why | Expensive to change? | Issue / PR |
|---|---|---|---|---|
| Layout | Flat package `vlah/` at the repo root, split by layer as in v2.2 §4.1 (`core`, `tools`, `ports`, `adapters`, `arms`, `runtime`, `cli`, `roles`, `prompts`, `testing`) | Matches the README and SDK names and the lint scopes in `.pre-commit-config.yaml` (`vlah/`, `tests/`); one owner per folder | Yes | #5 |
| Tooling | Formatting and linting through pre-commit (black, isort, ruff with default rules, autoflake, repo checks); pyright strict on `vlah/core/types` and `vlah/ports`, basic elsewhere; pytest with `--strict-markers` and a 60 s timeout | One toolchain for people and agents; strict typing where the contracts live | No | #5 |
| Testbed packaging | The testbed becomes a uv workspace member in the testbed's own PR, once `testbed/pyproject.toml` exists | uv rejects a workspace member without a `pyproject.toml` | No | #5, #10 |
| Specs | `docs/spec/*.md` are faithful English translations, and the Chinese originals live unchanged in `docs/spec/zh/`. All IDs and section numbers are unchanged. Terms follow one glossary (for example: blackboard, arm, spot check, fast/slow tier, start pack, pre-screening, top-level eval) | Contributors and agents read English; the originals stay as the source for checking a translation | No | #5 |
| Actor roles | `role_of_actor` reads the role from the whole actor id against a closed set of patterns (`worker-<n>`, `critic-<n>`, …, plus fixed ids such as `eval_service`); unknown ids raise. No role registry, and no `executor` role (the executor writes as the worker it runs) | Write permissions depend on it, so a loose prefix match must never grant a role | Yes | #6 |
| D34 priority | `merge_queue` ranks with the system writers (2) and `pi` with the planner (3); D34 does not list them | Needed for a total order | No | #6 |
| D9 result status | A result is verified once per ladder level as `verified:<source id>`; the types layer checks only the shape and the role, and the eval client owns the level order | The ladder is derived from source declarations at run time | No | #6 |
| D9 approach reinstatement | Reinstating an approach moves it back to `active`; planner or human | D9 says "→ reinstated" without naming the status or the role | No | #6 |
| D9 dead-end confirmation | Workers may move a dead end to `confirmed`; whether the worker is independent of the author is the indexer's check | D9: "or reproduced by a second independent worker" | No | #6 |
| Claim files | Claims are validated at write time: at least one evidence pointer, exactly one target per pointer, and the type's initial status only | D9 and D33 reject anything else anyway; failing early gives the writer the reason | No | #6 |
| Candidate hash | Follows TB§11.2 literally and hashes raw bytes, not a "normalized payload" (D69/D73): sorted POSIX relative paths, each file as UTF-8 path + NUL + raw bytes with no separator between files, the UTF-8 target id appended last, lowercase SHA-256 hex. Excluded: any `__pycache__` directory and any `*.pyc` file at any depth, and `manifest.yaml` at the top level only | It must match the testbed byte for byte; posted with a test vector on #10 | Yes | #6, #10 |
| Event payloads | The payload class is chosen by the event `type` from one table; events without a payload type must carry no data; `event_to_json` is the one serialization path and writes `from`, not `from_` | Several payloads share field names, so a guessed union member could be wrong | No | #6 |
| Params names | Durations end in `_seconds`, counts say what they count (`consecutive_failures_before_refresh`, `restart_tool_call_count`, `verdict_repeat_count`); the Integrator settings live in `Params` with the testbed's names (`quota_integrator_total`, `quota_integrator_per_hour`) | Coding style (units in names); one copy of each default; the same settings in every arm by construction | Yes | #6 |
| Arm switch table | No `integrator` field in `ArmConfig`; the field the issues called `ctx` is `context_composition` | Integrator settings must not vary by arm; coding style bans `ctx` | Yes | #6 |
| Limits and target | `charter/constraints.md` has a fenced `yaml limits` block (`eval_machine_hours`, `days`, `runs`), and `charter/progress.md` may have a `yaml target` block (`source`, `score`, `direction`) | Machine-readable without a second charter file | No | #6 |
| Token count | `count_tokens(text) = ceil(len(text) / 4)` everywhere | One approximation, so truncation budgets mean the same thing in every component | No | #6 |
| Randomness | Components take a seeded `random.Random` from the runtime; there is no wrapper type | The standard library type is enough (coding style: no thin wrappers) | No | #6 |
| Schema export | Dropped from F2: no generated JSON schema files | Nothing reads them, and the coding style discourages documentation-only artifacts | No | #6 |
