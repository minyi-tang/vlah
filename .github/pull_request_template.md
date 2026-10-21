<!--
Write for a reviewer who has not seen your conversation, notes, or earlier attempts.
Use short, plain sentences. Say what changes and why before how.
Delete these comments, and delete an optional section if it does not apply.
Guide: CONTRIBUTING.md, section "Writing the description".
-->

## Motivation

<!-- The problem this solves and why it matters now. One short paragraph in plain words. -->

## Modifications

<!-- What changes, grouped by idea rather than by file. Lead each point with the effect. Name a file only when it helps the reviewer find something. -->

## Related Issues

<!-- Optional. For example "Fixes #123" or "Closes #456". -->

## Testing

<!-- What you ran and what you saw. If the change affects speed, cost, or judge usage, give before and after numbers with units. For docs-only changes, say how you checked the rendering and links. -->

## Open Questions

<!-- Optional. Assumptions to confirm, risks, breaking changes, or decisions you want the reviewer to make. List every [proposed] choice you kept or changed, and every gap you filled (also recorded in docs/DECISIONS.md). -->

## Checklist

- [ ] `pre-commit run --all-files` passes.
- [ ] Added or updated tests for changed behavior.
- [ ] Updated documentation, docstrings, and examples as needed.
- [ ] The description makes sense to someone who has not seen the work.
- [ ] Touches only the paths my issue owns (see AGENTS.md), plus my own tests.
- [ ] If this changes a frozen contract (an `api.py`, `vlah/core/types/`, `vlah/ports/`, `vlah/arms/config.py`), it changes nothing else and has the `contract` label.
- [ ] For reviewers: If you haven't made any contributions to this PR and are only assisting with merging the main branch, please remove yourself as a co-author when merging the PR.
