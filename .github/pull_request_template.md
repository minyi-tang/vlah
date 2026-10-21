Closes #

## What this does

<!-- One or two sentences. Name the spec decisions it implements (D#, TB section). -->

## Checklist

- [ ] Touches only the paths my issue **owns** (see `CLAUDE.md`), plus my own tests
- [ ] Changes a frozen contract (`api.py`, `vlah/core/types/`, `vlah/ports/`, `vlah/arms/config.py`)? If yes, **this PR changes nothing else**, and it has the `contract` label
- [ ] Every `[proposed]` choice I kept or changed, and every gap I filled, has a row in `docs/DECISIONS.md`
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pyright` pass
- [ ] `uv run pytest` passes; my L0 assertions are no longer `xfail`; `pytest -m smoke` is green

## Decisions made while implementing

<!-- Copy the DECISIONS.md rows you added, or write "none". -->
