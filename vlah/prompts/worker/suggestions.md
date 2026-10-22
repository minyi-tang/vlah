These are suggestions, not rules.

- Search the blackboard before you submit a candidate: someone may have tried it.
- When an idea fails, record a dead_end whose evidence cites the candidate it
  tested (candidates/<hash>/...), so nobody runs it again.
- Use surprise for anything unexpected, even before you know what it means.
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
