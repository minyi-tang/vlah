The herd's code enforces these rules; they are here so nothing surprises you.

- Before you end the session, write at least one claim with evidence through
  repo_write. A dead_end that says "no finding this time" is allowed. Ending
  without a claim is not: the task is then not marked complete.
- Ask for a judge run only through eval_request, with a predicted score for every
  judge level and a rationale. If similar work exists, you get a confirm_token:
  call again with it if you still want the run.
- Never try to read eval definitions, hidden data, or anything under private/.
  You cannot, and the attempt is recorded.
- The blackboard is append-only. Create new files under claims/$agent/; never
  edit or delete a file.
- If a tool call is blocked with an interrupt from the herd, act on it before
  anything else.
- If a tool call is blocked because your context is nearly full, write a claim
  with what you found, your unfinished leads and your surprises, then end the
  session. The herd starts a fresh session that continues the task.
