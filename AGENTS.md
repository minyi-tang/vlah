# Agent guidelines

These rules apply to every coding agent working in this repository, including Copilot, Claude Code, and Codex. `CLAUDE.md` is a symlink to this file.

## Coding style

Before writing, modifying, or reviewing code, read and follow [docs/coding_style.md](docs/coding_style.md).

## Lint

Before every commit, run `pre-commit run --all-files` and fix every failure. [CONTRIBUTING.md](CONTRIBUTING.md#set-up-lint) explains the checks.

- Never skip or weaken a check to get it to pass: no `--no-verify`, no `SKIP=`, and no edits to `.pre-commit-config.yaml` or `scripts/check_*.py` unless that is the task.
- Fix violations of the custom checks by hand. Don't run `scripts/check_leading_underscore.py --fix` (it misses callers in other files), and use `else: pass` only when the other branch really does nothing.

## Pull requests

Before you open a pull request, read [Pull requests in CONTRIBUTING.md](CONTRIBUTING.md#pull-requests) and follow it. In short:

1. Work on a branch and never commit to `main`. Keep one change per pull request.
2. Make sure `pre-commit run --all-files` passes and that tests and docs are updated.
3. Write the title as a plain imperative sentence that says what the pull request does.
4. Fill in every required section of [the pull request template](.github/pull_request_template.md). Delete its comments and any optional section you don't use.
5. Before you submit, reread the description as a reviewer who has not seen your session. If they couldn't tell what changed and why within 30 seconds, rewrite it.
6. After creating the pull request, open it and check that the description renders: real line breaks rather than literal `\n`, and no leftover template comments.
7. When later commits change the scope, update the title and description.

Agents get these wrong most often:

- **Narrating your process.** Describe the result, not the steps you took ("First I explored the repo...").
- **Pasting tool output.** Don't paste transcripts, raw logs, or full diffs. Quote only the lines that matter.
- **Unverified claims.** Name the commands you ran. Never write "all tests pass" unless you ran them, and say what you could not run.
- **Hidden decisions.** List every assumption or choice the user didn't make explicitly under Open Questions.
- **Filler.** Leave out praise, hype words, emoji headings, and closing summaries that repeat the sections above.
