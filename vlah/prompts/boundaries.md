The herd enforces these boundaries. Within them, choose how you work.

- Before you end the session, write at least one claim with evidence. A dead_end
  tagged no_finding that says "no finding this time" is allowed. Ending without a
  claim is not: the task is then not marked complete.
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
