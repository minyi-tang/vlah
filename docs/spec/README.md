# Specs

These are the design documents VLAH is built from. The files in this folder are **English translations**. The Chinese originals are in [`zh/`](zh/), unchanged.

The translations are faithful and complete, and every ID and section number is unchanged: D1–D79, H#, P#, C#, §, TB§. The issues therefore cite the same numbers in both languages. Each issue also quotes, in English, the parts its component needs. Come here when an issue cites an ID and you want the full context.

| File | What it is | Cited as | Original |
|---|---|---|---|
| [research-herd-design-v2.2.md](research-herd-design-v2.2.md) | **The herd design.** Principles P1–P10, hypotheses H1–H13, the decision register D1–D79 (§15), the L0 assertions (§14.1), and the implementation brief (Appendix C) | v2.2, D#, H#, § | [zh](zh/research-herd-design-v2.2.md) |
| [herd-testbed.md](herd-testbed.md) | **The testbed design.** Problems A and B, the slow tier, the recorder, the scorecard, and the implementer spec (§11) | TB§ | [zh](zh/herd-testbed.md) |
| [one-page-alignment.md](one-page-alignment.md) | The one-page summary: the four invariants, the build order, and decisions still open (P11, zero roles, problem C last) | one-page | [zh](zh/one-page-alignment.md) |
| [research-herd-v2.2-fixes.md](research-herd-v2.2-fixes.md) | The review that produced round 30 of v2.2 (the spot-check and interaction thresholds, one merger shared by every arm). It is already applied to v2.2, and kept as a record. Its line numbers refer to the Chinese v2.2 file | fixes review | [zh](zh/research-herd-v2.2-fixes.md) |

## If the translation and the original disagree

The Chinese original is the source of the design. If a translated passage looks wrong or ambiguous, check `zh/`. Then fix the translation in a small PR that changes nothing else, and mention it in the issue you're working on.

## Known inconsistencies in the originals

The translators found these, and translated each as written. Until @minyi-tang resolves them, follow the issue for your component, which already takes a position:

| Where | Inconsistency | What the issues assume |
|---|---|---|
| v2.2 D70 vs §14.1 | D70 says "L0 plumbing, 12 assertions". §14.1 lists **20** | 20 (#9) |
| v2.2 §14.1 (L1) vs D72 | §14.1 splits the data into **four** parts (public, daily, spot check, verdict). D72 says **three** | The herd uses three (daily, `holdout_run`, `holdout_verdict`); the testbed's packed *public* split is the fourth (TB§11.3) |
| v2.2 §0.1 row 30 vs §14 | Row 30 says "the weekly plan does not roll over and is only illustrative". §14 says "if it isn't enough, extend" | The weeks are illustrative. Progress follows the gates, not the calendar |
| TB§11.3 | Under `per_actor_share`, it doesn't say whether "total `ceil(96/N)`" is per actor or for the whole arm | Open: the testbed agents decide and record it in `testbed/DECISIONS.md` (#10) |
| v2.2 §11 (H2 criterion) | "Higher than the summary group by 2× or more" doesn't say 2× of what (the mean score, or the gain over the original) | Open: H2 is Wave 3 |

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
