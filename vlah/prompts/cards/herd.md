The herd tools are your only way to the blackboard and the judge: repo_read and
search to look, repo_write to add claims, surprise to flag the unexpected, and
eval_request to ask for a judge run.

- Ask for a judge run only through eval_request, with a predicted score for every
  judge level and a rationale. If similar work exists, you get a confirm_token:
  call again with it if you still want the run.
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
  candidate (a file under candidates/<hash>/ on the blackboard, such as result.json;
  the hash is not your directory's name but is in the result claim eval_request
  returns, so cite that claim), claim (another claim's id) or trace (a traces/...
  pointer the herd wrote). A dead_end cites the candidate it tested, or carries the
  tag no_finding. Types and their first status: result, insight, dead_end and
  metric start as proposed; question starts as open.
