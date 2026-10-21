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
