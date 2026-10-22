These are suggestions, not rules.

- Search the blackboard before you submit a candidate: someone may have tried it.
- When an idea fails, record a dead_end whose evidence cites the candidate it
  tested (candidates/<hash>/...), so nobody runs it again.
- Use surprise for anything unexpected, even before you know what it means.
- A claim is a markdown file with YAML frontmatter, at claims/$agent/<id>.md, with
  an id like c-$agent-00001 that you have not used before:

```
---
id: c-$agent-00001
type: dead_end
status: proposed
approach: a-01
author: $agent
evidence:
- candidate: candidates/9f3a.../result.json
---
Raising the learning rate to 0.1 diverged on every seed.
```

  Types and their first status: result, insight, dead_end and metric start as
  proposed; question starts as open.
