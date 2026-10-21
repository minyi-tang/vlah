# Decision log

Record every implementation decision the spec leaves open here: every `[proposed]` item from an issue that you kept or changed, and every gap you filled. Never decide silently (v2.2 Appendix C).

Before adding a row, check v2.2 §15 (`docs/spec/research-herd-design-v2.2.md`): the answer may already be there. The testbed keeps its own log in `testbed/DECISIONS.md`.

**Expensive to change?** means: would changing this later force a contract PR, a data migration, or invalidate runs that already happened?

| D# or topic | Decision | Why | Expensive to change? | Issue / PR |
|---|---|---|---|---|
| Layout | Python package `vlah` in `src/vlah/`, split by layer as in v2.2 §4.1 (`core`, `tools`, `ports`, `adapters`, `arms`, `runtime`, `cli`, `roles`, `prompts`, `testing`) | Matches the README and SDK names; one owner per folder | Yes | #5 |
| Tooling | uv, ruff (lint + format), pyright (strict on `core/types` and `ports`, basic elsewhere), pytest with `--strict-markers` and a 60 s timeout | One toolchain for every agent; strict typing where the contracts live | No | #5 |
| Markdown | `ruff format` skips `*.md` | The docs show the planned interface; code blocks there are illustrations | No | #5 |
| Testbed packaging | The testbed becomes a uv workspace member in the testbed's own PR, once `testbed/pyproject.toml` exists | uv rejects a workspace member without a `pyproject.toml` | No | #5, #10 |
