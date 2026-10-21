# Specs

These are the design documents VLAH is built from, copied verbatim and written in Chinese. The issues quote, in English, the parts each component needs. Come here when an issue cites a D#, H#, § or TB§ and you want the full context.

| File | Original name | What it is | Cited as |
|---|---|---|---|
| [research-herd-design-v2.2.md](research-herd-design-v2.2.md) | Research-Herd-Design-v2.2.md | **The herd design.** Principles P1–P10, hypotheses H1–H13, the decision register D1–D79 (§15), the L0 assertions (§14.1), and the implementation brief (Appendix C) | v2.2, D#, H#, § |
| [herd-testbed.md](herd-testbed.md) | Herd-Testbed.md | **The testbed design.** Problems A and B, the slow tier, the recorder, the scorecard, and the implementer spec (§11) | TB§ |
| [one-page-alignment.md](one-page-alignment.md) | Research-Herd-一页对齐.md | The one-page summary: the four invariants, the build order, and decisions still open (P11, zero roles, problem C last) | one-page |
| [research-herd-v2.2-fixes.md](research-herd-v2.2-fixes.md) | Research-Herd-v2.2-fixes-zh.md | The review that produced round 30 of v2.2 (the spot-check and interaction thresholds, one merger shared by every arm). It is already applied to v2.2, and kept as a record | fixes review |

## Which source wins

1. **Merged code**, specifically the frozen contracts (`api.py`, `vlah/core/types/`, `vlah/ports/`, `vlah/arms/config.py`), and then `docs/interfaces.md`.
2. **The issue** for your component. It quotes the spec, and marks the gaps it fills as `[proposed]`.
3. **v2.2** for the herd. **TB§11** for the testbed, as adjusted by issue #10: the testbed lives in this repo.
4. **The one-page alignment** records where decisions are still pending. It doesn't override v2.2 until @minyi-tang approves the change.

If something isn't defined anywhere, check v2.2 §15 first. If it still isn't there, add a row to [`../DECISIONS.md`](../DECISIONS.md). Never decide silently.

## Not copied

These stay outside the repo:
- earlier versions (v2, v2.1, v3) and the v3 review;
- the `herd-sim/` simulation scripts that D72 refers to.

Ask @minyi-tang if you need them.

## Caution for later work

`herd-testbed.md` describes the planted structure of the testbed problems: their ingredients, the trap, the conflict pair and the planted bugs. That is fine while herd workers only receive packed problem folders. **When the herd starts working on its own code (problem C, one-page §4), its workers must not see `docs/spec/` or `testbed/`.** Otherwise they could learn the testbed's answers.
