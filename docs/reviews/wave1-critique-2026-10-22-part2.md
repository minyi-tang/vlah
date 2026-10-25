# Wave-1 critique, part 2: the harness, the worker loop and the runtime

An adversarial review of what merged after the first critique: the Copilot harness and worker loop (W1-08, PR #48, with #53 and #55), the herd runtime (W1-11, PR #54), and the raising-subscriber fix (#52). Reviewed on `main` at `11b97ff`. Every finding marked **reproduced** was run against the real components with a script in this session's scratchpad. Nothing was edited or committed.

The earlier fixes hold: the probes for the seven blocking findings of #43 were rerun at `c716fe2` and no longer reproduce, and #54 and #55 touch neither the blackboard, the queue nor the eval client; the S3 probe was rerun at `11b97ff`. The suite passes there (260 passed, 10 skipped, 10 xfailed, all owned by components not yet merged). The live Copilot end-to-end test, the one path in #48 that needs the real CLI, was not rerun by this review.

## Blocking before a real run

### H1. Copilot's compaction is read as a replaced process, so the D77 restart never fires. Reproduced.

`vlah/core/worker/loop.py:259-260` clears `over_threshold_since` whenever the context fraction drops, on the assumption that within one process the context only grows. Copilot compacts in the background at 80% of the window (DECISIONS row 127), and the transcript reader then reports the post-compaction size. `compactions()` in `vlah/adapters/harness/copilot/transcript.py:110` detects the compaction event, but nothing calls it. Scenario: the agent reaches 75%, the hook denies everything but the wrap-up tools, the agent keeps reading and searching, Copilot compacts at 80%, the next usage record says 20%, the hook lets everything through again, the loop clears its kill clock, and the agent works on for over an hour on a lossy context with no claim and no restart pack. That is the outcome D77 and P2 exist to prevent, and the issue's done-when item "build() output contains the disabled compaction" is neither met nor tested. Fix shape: a drop on the same transcript path is a compaction; on the compaction event, kill and restart from the D77 pack, and keep the hook denying.

### H2. The restart decision depends on a 30-second poll having seen the crossing. Reproduced.

`loop.py:212` restarts on exit only if `over_threshold_since` was set, and that happens only inside `watch_context`, which runs only when `executor.wait` times out. An agent that crosses 70% and does what the hook tells it, writes its contribution claim and exits within one poll, is marked complete, and its "unfinished leads" are never continued. L0-18a passes because its scripted agent takes 120 seconds to wrap up. Fix: re-read the usage at exit and restart on the fraction alone.

### H3. The hook's context guard fails open whenever the tool server is slow or down. Reproduced.

`vlah/adapters/harness/interrupt_hook.py:102` posts to `/hook/check` first with a two-second timeout; any exception lets the call through before the context check at `:112` runs. The two checks are independent and the context one needs no network. With the server unreachable and the transcript at 75%, Bash is allowed. Failing open on interrupts is defensible because the executor backstops it; failing open on D77 is not, and with H1 and H2 the loop's own backstop is unreliable too.

### R1. A worker-loop crash mid-session leaves the session alive and unleased, and the task runs twice. Reproduced.

`vlah/runtime/supervisor.py:83-102` restarts a crashed slot without touching the crashed loop's session; `loop.py:176-180` releases the lease and requeues the task in `finally` but never kills the handle; `supervisor.py:185-207` kills only leased handles, so the orphan survives both stop and finish. Any exception out of the session-following loop, such as `executor.wait` raising once, produces a second session for the same task while the first keeps running with a registered sandbox and token, and its later writes land after closeout. The crash test starts no session, so it cannot see this.

### R2. A compose agent's result follow-up lands in the arm queue and is claimable by any arm worker. Reproduced.

`vlah/runtime/herd.py:81-82` sends every `result_ready` to the arm queue regardless of submitter; the follow-up is built with the default `pool="arm"` and `for_agent=worker-00`, which the compose slot never reads and which opens to any agent once `worker-00` holds no lease. In B1 a worker would then be handed compose work, which D74 says must not happen. Latent until the Integrator (#19) issues compose tasks.

### R3. The fan-out hands the Integrator its own remeasure claims as verified. Reproduced.

`herd.py:83-88` calls `on_verified` for every eval-service `claim_status` to `verified:<top>`; a remeasure job writes the same status for its claim before `remeasure_ready`, and the event carries no tag. One spurious verified candidate per baseline advance unless #19 filters its own claims. Latent until #19.

## Serious

- **R4. The S1 mirror carries every worker's rationale.** `vlah/runtime/isolation.py:35-50` keeps `claims/eval_service/`, and a result claim's body is the submitter's rationale. S1's own tool scope refuses the same path. S1 exists to share scores without insight; the clone shares the insight. Reproduced; the isolation test writes no eval-service record.
- **R5. The compose agent's setup is arm-dependent.** It uses the arm's tool server and the run's eval service, so its `eval_request` gets the sync-point diff in S1 and S2 and not in B1, and dedup and confirmation apply by arm. D74 says compose is the same in every arm. Reproduced.
- **R6. The runtime re-derives the top level and disagrees with the ladder on ties** (`herd.py:55-63` takes the first of equals, the eval service the last), so with two judges of equal cost and latency `on_verified` never fires and the Integrator merges nothing all run. Reproduced. The eval service owns that value and should expose it.
- **H4. The worker loop widens the frozen harness and executor ports without a contract PR.** `loop.py:46-72` defines `TranscriptReader`, `WorkerHarness`, `SessionFiles` and `AgentRegistry`, which the runtime adopts; the reference `ScriptedHarness` no longer satisfies what the loop needs, and `check_harness` no longer certifies it. D61 says to promote a leaked capability to an explicit port field in a contract-only PR.
- **H5. Nothing sets `AgentSpec.egress`.** The executor substitutes an empty allowlist. Harmless under the passthrough sandbox; the first real sandbox will block the MCP server, the hook endpoint and the slow-eval host, the three the issue says must stay reachable.
- **H6. The D77 pack carries only overdue interrupts.** `loop.py:220` sets `last_interrupt` only from the inbox; hook-delivered interrupts, the common path, never reach the pack, and once set the field is never cleared.

## Lower severity and undecided

- The S1 mirror excludes `baseline/`, `map/`, `digests/` and `events/integrator/`, while D58 gives S1 baseline advances and the updated map; an S1 worker gets a rule-6 interrupt about a baseline it cannot read. No DECISIONS row.
- B0 is "one agent" in D58, but `build_herd` accepts any worker count for it.
- Overdue interrupts are keyed by agent, not task, so a late one can restart the next task's session with a stale interrupt.
- The harness never checks which model actually ran. DECISIONS row 124 records that every session ran `gpt-6.1-sol` because of an account setting, whatever the flag said. P10 wants one strongest model for every role; at minimum log a mismatch per session.
- The hook command bakes the herd's interpreter path into the sandbox's settings; inside a real sandbox that path may not exist, and Copilot treats a hook error as a denial.
- The executor's no-claim restart and the loop's D77 restart do not know about each other.

## Tests that cannot fail

- L0-18a encodes a 120-second wrap-up, so a quick wrap-up is never tested (H2).
- L0-19's Integrator equality is tautological beyond `params is params`; the fake stores the object.
- L0-10's router and indexer checks exercise fixtures that honour the arm, as the PR acknowledges.
- The runtime crash test crashes before any session starts (R1); the isolation test writes no eval-service record (R4).
- The done-when item "disabled compaction" has no test, and the feature does not exist (H1).

## Decisions the maintainer owes

- **The model.** Every Copilot session ran `gpt-6.1-sol` under an administrator setting. Either confirm it as the one strongest model (P10) or change the setting before any measured run.
- **Compaction.** Copilot cannot turn compaction off outside its SDK. Either accept H1's fix shape (treat compaction as a restart trigger at 70%) or drive Copilot through the SDK with infinite sessions disabled.
- **B1 per-worker repositories.** D67 says each B1 worker gets its own repository; the runtime uses per-worker mirrors of one blackboard with one eval service. The PR asks for confirmation.
- **Copilot instead of Claude Code.** Recorded in DECISIONS row 122; #41 stays a draft.
