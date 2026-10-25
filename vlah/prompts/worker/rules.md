These boundaries and tool protocols are required. Within them, choose how you
investigate.

- Before you end the session, write at least one claim with evidence through
  repo_write. A dead_end that says "no finding this time" is allowed. Ending
  without a claim is not: the task is then not marked complete.
- Ask for a judge run only through eval_request, with a predicted score for every
  judge level and a rationale. If similar work exists, you get a confirm_token:
  call again with it if you still want the run.
- Never try to read eval definitions, hidden data, or anything under private/.
- Work only from your own clone and what the tools return. Never try to reach
  another agent's sandbox, clone or files; another worker's claims come to you
  through the tools, or not at all in arms that do not share.
- The blackboard is append-only. Create new files under claims/$agent/; never
  edit or delete a file.
- If a tool call is blocked with an interrupt from the herd, act on it before
  anything else.
- If a tool call is blocked because your context is nearly full, write a claim
  with what you found, your unfinished leads and your surprises, then end the
  session. The herd starts a fresh session that continues the task.

## Candidate and claim formats

- A candidate is a fresh directory holding only the files to evaluate plus
  manifest.yaml. Copy those files into it; never submit target/ or any other git
  checkout, since a candidate that contains .git is refused.
- A claim is a markdown file with YAML frontmatter, at claims/$agent/<id>.md, with
  an id like c-$agent-00001 that you have not used before:

```
---
id: c-$agent-00002
type: insight
status: proposed
approach: a-01
author: $agent
evidence:
- path: spec.md
- candidate: candidates/9f3a.../result.json
---
The cache misses dominate: halving them doubled the throughput on every seed.
```

  Each evidence item has exactly one key: path (a file on the blackboard),
  candidate (a candidate's directory or result), claim (another claim's id) or
  trace (a trace pointer). Types and their first status: result, insight,
  dead_end and metric start as proposed; question starts as open.
