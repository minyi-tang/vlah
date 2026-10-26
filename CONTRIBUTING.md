# Contributing to VLAH

The most useful contribution right now is a real problem; see [Contributing](README.md#contributing) in the README. This page covers changes to code and docs. The rules apply to people and to coding agents. Agents also follow [AGENTS.md](AGENTS.md).

## Set up lint

All checks live in [.pre-commit-config.yaml](.pre-commit-config.yaml). CI runs the same checks on every pull request, and a pull request with a failing check is not merged.

```bash
pip install pre-commit    # or: uv tool install pre-commit
pre-commit install        # run the checks on every commit
pre-commit run --all-files
```

Besides the standard formatters (black, isort, ruff, autoflake), three checks are specific to this repository:

| Check | Rule | Scope |
| --- | --- | --- |
| `scripts/check_leading_underscore.py` | No leading `_` on class, function, or attribute names | `vlah/`, `tests/` |
| `scripts/check_if_else.py` | Every `if` ends in an `else` | `vlah/` |
| `scripts/check_doc_headings.py` | Exactly one `#` heading and nothing deeper than `###` | `docs/` |

The checks only report. Fix the code by hand. The `--fix` options are a last resort: the underscore fix misses callers in other files, and `else: pass` is not a substitute for a real `else` branch.

## Coding style

Before you write or review code, read [docs/coding_style.md](docs/coding_style.md).

## Pull requests

For package version updates and operator-only publication, follow
[Releasing the Python packages](docs/releases.md).

### Before you open one

- Work on a branch. Never commit to `main`; a pre-commit check blocks it.
- Keep one change per pull request. Split unrelated fixes and refactors into their own pull requests.
- Run `pre-commit run --all-files` and fix every failure. Don't skip hooks.
- Add or update tests for changed behavior, and update docs that describe it.

### Title

Write the title as a plain sentence that says what the pull request does, in the imperative.

- Good: "Skip judge runs for candidates that repeat a dead end"
- Not: "Dedup fix", "Update eval_service.py", "[WIP] misc changes"

Use sentence case and no trailing period, and keep it under about 70 characters.

### Writing the description

Fill in [the template](.github/pull_request_template.md). The reader is a reviewer who has not seen your conversation, notes, or earlier attempts. After 30 seconds they should know what changed, why, and what they need to check.

- **Start with why.** Motivation states the problem before the solution.
- **Describe behavior, not files.** Group changes by idea, and lead each point with its effect. Name a file only when it helps the reviewer find something.
- **Use plain words.** Write short sentences in the active voice, and use full words instead of abbreviations. Define any term the reader may not know. Don't use internal shorthand such as design-point IDs or branch names without saying what it means.
- **Be concrete and honest.** Say exactly what you ran and what you saw. Give numbers with units and a baseline. Never claim a test or check you did not run, and say what is not covered.
- **Surface what needs a decision.** Put assumptions, risks, breaking changes, and open choices under Open Questions, where the reviewer can't miss them.
- **Keep it short.** Most descriptions fit on one screen. Don't restate the diff, paste long logs, or narrate how you worked.
- **No filler.** Leave out hype words ("comprehensive", "robust", "seamless", "successfully"), emoji headings, and sign-offs.
- **Format for scanning.** Use the template's sections, short bullets, and code formatting for commands, paths, and identifiers. Delete the template comments and any optional section you don't use.

[#1](https://github.com/minyi-tang/vlah/pull/1) and [#2](https://github.com/minyi-tang/vlah/pull/2) show the tone to aim for.

### Example

Here is a description for a made-up change:

```markdown
## Motivation

Workers sometimes submit a candidate that is identical to one already recorded
as a dead end. Each repeat costs a 30-minute judge run and teaches us nothing.
In last week's testbed run, 9 of 28 judge runs went to repeats.

## Modifications

- The eval service now compares each candidate with the recorded dead ends
  before it books a judge run. A repeat is rejected with a link to the
  earlier result.
- The worker sees the rejection on the blackboard, so it moves on instead of
  retrying.
- "Identical" means the same normalized patch. Near-duplicates still go
  through, because that needs a similarity threshold we haven't agreed on.

## Testing

- `pytest tests/test_eval_service.py` passes, including a new case that
  submits a repeat and checks that no judge run is booked.
- Replayed last week's testbed log: judge runs dropped from 28 to 19, and
  the final score was unchanged.

## Open Questions

- Should a rejected repeat count against the worker's budget? This PR says no.
```

The same change described badly:

```markdown
## Summary

I have successfully implemented comprehensive deduplication logic! 🎉

- Modified eval_service.py
- Modified blackboard.py
- Added tests/test_dedup.py

All tests pass.
```

It never says why the change is needed or how it behaves. It lists files instead of effects, and "All tests pass" doesn't say which tests ran.

### Keep it current

If later commits change what the pull request does, update the title and description to match. Reply to review comments in the review threads, not by editing the description.
