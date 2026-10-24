# Wave-1 critique, 2026-10-22

An adversarial review of the four merged Wave-1 components (#11, #13, #14, #16) and the work-in-progress local executor (#17), against their issues, v2.2 and TB§11. Every finding marked **reproduced** was run against the real adapters with a probe script in this session's scratchpad; the rest are reasoned from the code with the confidence stated. Nothing here was edited or committed; all paths are repo-relative and line numbers are from `main` at `9aac3de` (PR #39 merged) and the `wip executor` commit `0732e3d` on `w1-07-executor`.

The headline from the first pass still holds: each closed issue's L0 tests pass, the 14 xfails map to open issues, and lint, types, smoke and unit tests are green. What the gates do not catch is below.

## Blocking before any real run

These break a stated invariant or make a spec mechanism unreachable.

### B1. The pre-receive hook is not a push credential; a worker can push to main and wedge the store (D68, D56). Reproduced.

`vlah/adapters/store/git/store.py:31-40` guards only the default `git-receive-pack`. Over `file://` the client picks the server program, so a worker clone can run `git push --receive-pack='git -c core.hooksPath=/nonexistent receive-pack' origin HEAD:main` and the forged file lands on `main`. `git --git-dir=<bare> update-ref` does the same. Afterwards the store's in-memory `main_head` (`store.py:255-277`) is stale, every later `fast_import` raises `Not updating refs/heads/main`, and the herd cannot write until restart, when `rebuild_index` adopts the forged commit. Precondition: filesystem write access to the bare repo, which the local executor gives every worker (same uid) unless W1-12 denies it at the OS level. PR #32's Open Questions already flagged that filesystem access defeats the hook and pushed it to #17 and #22; what is new here is that the push transport itself is bypassable without touching the filesystem directly, and that the store wedges afterwards. DECISIONS row 48 ("effectively read-only") overstates the guard. The executor's `refresh_blackboard` (`vlah/adapters/executor/local/workspace.py:147`) also writes the bare repo's path into the clone's `.git/FETCH_HEAD`, so the sandbox is handed the path the module docstring says it never gets.

Fix direction: the bare repo owned by the tool-server uid with workers denied write; `fast_import` re-reads `refs/heads/main` and fails loudly instead of wedging.

### B2. A write can delete existing files through a blob/tree name clash (D31 append-only). Reproduced.

`vlah/core/blackboard/blackboard.py:165` and `store.py:189,315-316` check append-only by exact path, but git cannot hold a blob and a tree at one name. worker-02 writing the single file `candidates/abc/candidate` after worker-01 wrote `candidates/abc/candidate/foo.py` merges, and `foo.py` is gone from `main` (`git log --stat` shows the deletion). The reverse order deletes the file. `list_paths` then still lists the deleted path, `read` raises `FileNotFoundError`, and a rewrite is rejected `file_exists`. The memory reference store is a flat dict, so `check_versioned_store` passes on both adapters while their semantics differ. Fix: reject any path that is a prefix of an existing path or has an existing file as an ancestor, in `check_one` and in `merge`.

### B3. `tick` has no per-job fault isolation; one bad judge reply stalls every eval job (invariant 3). Reproduced.

`vlah/core/eval/service.py:400-414` loops over jobs with no `except`; a failing job keeps its state and re-raises every tick, and nothing behind it advances. Three verified triggers: a testbed `failed` ticket (`client.py:206` returns empty `per_seed`, `pairing.py:26` raises); a `done` result with a timed-out seed (testbed `problems/sched/problem.py:260` puts `None` in `per_seed`, `SlowTierAggregate` rejects it, and TB§11.3 says this is a normal outcome); any 400 from `/submit` (`client.py:189-191` raises `ValueError`, the job is resubmitted every tick). Requirement 12's retries with backoff are absent and no DECISIONS row records the omission. Fix: catch per job, finish it as `refuted` or `failed` with a `claim_status`, continue.

### B4. Every Integrator remeasure is rejected by the real judge (D74). Reproduced on the manifest bytes.

`service.py:364` queues remeasures with `predictions={}`; `client.py:119-132` emits `predicted_score: null`; the testbed's `slow_eval/core.py:411-413` answers `400 missing_prediction`. Through B3 the first remeasure then poisons `tick` for everything behind it. Fix: carry the parent's last measured score or the Integrator's expected gain as the prediction, or agree in #10 that `integrator` is exempt.

### B5. A post-result gate failure still produces a score (D54, L0-7). Reproduced.

`service.py:432-486`: the claim becomes `refuted` (correct), but `finish` still writes `result.json` with the full paired block and emits `result_ready` with `score=3.0`, `paired.mean=3.0`, `constraint_ok=True`. `ResultReady` has no refuted marker and the queue creates the submitter's follow-up unconditionally, so the worker is told it gained. `score.value or 0.0` (`service.py:512,530`) also turns a void result into an absolute score of zero. L0-7 covers only the pre-submit gate.

### B6. `reclaim_stale` requeues a task while keeping the old lease (D32, v2.2 §15 "two leases for the same task"). Reproduced.

`vlah/core/queue/queue.py:363-389`: the reclaimer snapshots the lease, asks the executor, and if a heartbeat lands in that window the equality check fails, the lease is kept, but the task is requeued and `lease_stale` emitted anyway. Result: two leases on one task; the orphan's marker key never changes so later passes skip it; the orphan blocks `acquire` for that worker and counts as a running slot in `maintain`. The equal branch is also two store operations (compare, then delete), so a beat between them deletes a fresh lease. Root cause: the frozen `KeyValueStore` has no compare-and-delete. `requeue` (`queue.py:159-168`) has no lease guard either, so a public requeue of a held task gives the same two-lease state.

### B7. `pool="external"` tasks reach arm workers whenever both queues share a store (D74). Reproduced on memory and fakeredis.

`queue.py:86-88` uses one sorted-set key for every pool and `claim_task` (`:141-147`) never checks `task.pool`. D67 gives one key-value namespace per run, so a shared store is the expected wiring. A foreign entry also suppresses the exploration fallback (`:178,197`). The unit test builds the external queue on a second store, so it cannot see this.

## Serious, fix before W1-08 wires real workers

### S1. The hook endpoint and MCP auth take HerdTools' global lock (D17, D60 layer 2, #16 "under 50 ms"). Reproduced over HTTP.

`vlah/tools/server.py:36,141` call `agent_for_token`, which is `with self.lock` (`herd_tools.py:150-152`), on uvicorn's event-loop thread. `write_locked` (`:292-346`) holds that lock across the whole `blackboard.write` (git subprocesses plus listener fan-out) and across the blocking recorder POST (`:366-384`, 30 s timeout). With worker-01 in a 2 s recorder call, worker-02's `/hook/check` took 1803 ms instead of 29 ms, and the event loop itself blocks, so every agent's MCP auth stalls. The module docstring and the DECISIONS row both say the hook takes no lock. Second effect: a recorder non-200 raises after the claim merged and the lease was updated, and a retry gets `file_exists`; a recorder outage also fails `repo_read` and `search`.

### S2. A file symlink inside `candidate_dir` copies any host file the eval client can read onto the shared blackboard (D63). Reproduced.

`herd_tools.py:425-433` resolves and checks the directory only; `vlah/core/types/candidates.py:38-40` and `vlah/core/eval/records.py:71` follow file symlinks. `sandbox/cand/leak.yaml -> /outside/herd.yaml` is hashed and written as `candidates/<hash>/candidate/leak.yaml`, visible to every S1/S2 worker. Matters once the eval client runs on the orchestrator host and the worker in a sandbox. Reject any symlink in the candidate directory with an `lstat` walk, on both sides.

### S3. A raising blackboard subscriber drops the commit for later listeners and raises into an unrelated writer. Reproduced.

`blackboard.py:322-342`: the drainer is whichever writer last left the merge lock; the commit is popped before listeners run, an exception propagates out of that writer's `write` although its files are on `main`, and later listeners never see the sequence number. Contradicts #11's at-least-once delivery. An indexer bug would therefore corrupt the router's view silently.

### S4. `list_paths` races with merges (`Set changed size during iteration`). Reproduced.

`store.py:299-303` iterates `main_paths` without the lock. Callers on other threads exist today: `herd_tools.py:232,389`, `eval/records.py:63,94`, `eval/service.py:115,158`, `executor/local/traces.py:80,123`.

### S5. Replaying a `result_ready` after its follow-up completed raises a bare `AssertionError` inside the merge fan-out. Reproduced.

`queue.py:227-231` with `complete` deleting `task/<id>` at `:157`. The caller is `CommitFanout.__call__` (`vlah/runtime/wiring.py:53-54`) inside `blackboard.merge`, so a rebuild that re-feeds events crashes it, and through S3 it takes other listeners with it. L0-5a only replays before the task is claimed.

### S6. PR #39's lock is wider than described.

The RLock (`service.py:119`) is held across all of `tick` (`:401`): every per-job `/submit` and `/status` call with a 30 s timeout, the `/meta` GET inside `score_level`, and post-result gate runs. One slow poll blocks every worker's `eval_request`. The #16 DECISIONS row still says "a slow gate must not hold up other workers" while the new row does exactly that. Acceptable as a labelled stopgap; the proper shape is a hash reservation under the lock, gates and I/O outside it.

### S7. Eval client bookkeeping is in-memory only, and cache hits are billed. Reproduced (restart); reasoned (duplicate flag).

`jobs_by_hash`, `QuotaTracker` and `jobs` are not rebuilt from the blackboard, so after a restart the same candidate is queued and charged again and in-flight tickets are orphaned (`EvalRecords` does rebuild counters, so restart was considered). The testbed's `duplicate: true` reply is parsed (`client.py:56-59`) and dropped (`:178-180`), so a cache hit is charged to quota and budget, and `pair_per_seed(..., 1)` at `service.py:431` hard-codes `n_reps=1`, which makes L0-13's "n_reps doesn't count cache hits" vacuous. Surfacing the flag needs a `SignalSource` contract change.

### S8. An unknown baseline spends a run and is held forever. Reproduced.

`service.py:152-178` never checks that `request.baseline` resolves; `baseline@42` is queued, the judge run and budget are spent, and `score_level` holds at `:425` with no event. Separately, when `baseline` is omitted the service uses the current baseline (`:182`) while the worker's `tb fast` hashed on `manifest.target`; after the baseline advances the hashes differ and the quota-discipline metric counts the worker as undisciplined. No DECISIONS row reconciles §7's default with D69's manifest target.

### S9. `repo_write` raises on a malformed claim instead of returning violations, and drops the interrupt. Reproduced over HTTP.

`herd_tools.py:301-305` pre-parses every `claims/` path; `ValueError` and `ValidationError` propagate as `Error executing tool repo_write` with no violation and no `interrupt` field (#16 requirements 3 and 8). `claims/worker-01/notes.txt`, which the blackboard accepts, is unwritable through the tool.

### S10. Bearer tokens come from the seeded injected `Random`. Reproduced.

`herd_tools.py:141` uses `self.random.getrandbits(128)`; two harnesses seeded alike issue the same token. House rule 4 pushes the runtime toward a fixed seed, and "the token alone decides who is calling", so a worker could impersonate a peer. The push token already uses `secrets` (`layout.py:30,46`); do the same here. Determinism is not needed for credentials.

## Executor work in progress (#17, commit `0732e3d`)

Eleven unit tests and L0-18b pass; the real-time conformance test is marked `slow`. The files appeared in this checkout while this review ran, so this is a snapshot.

- **X1. The target worktree shares the source repository's `.git`.** `workspace.py:121-130` uses `git worktree add`, so the worker holds `gitdir: <target_repository>/.git/worktrees/...` and can write that repository's refs and objects (verified: `update-ref` from the worktree created a branch in the source). Requirement 0 says the sandbox must not write shared state; this is the same shape as B1 for the target instead of the blackboard. Clone instead, or point workers at a per-run read-only mirror.
- **X2. `after_exit` can launch two restarts.** `executor.py:297-309` launches the restart, then uploads the trace; if `write_trace` raises (an `ObjectStore` or blackboard I/O error, not a rejected receipt) the new process is never recorded, the old exit is reaped again next tick, and a second attempt-2 process is started. Reasoned, not reproduced.
- **X3. Workers inherit `HOME`.** `workspace.py:28-40` passes `HOME`, so the worker sees the host user's git credential helpers, `~/.netrc`, `~/.ssh` and the harness login. Only `VLAH_PUSH_TOKEN` is filtered. The harness needs a home for its own login, so this is a decision to record under D68, not a one-line fix.
- **X4. Executor stubs never reach the testbed recorder.** The stub dead end goes through `blackboard.write` directly (`traces.py:172`), and the only recorder client lives in `vlah/tools/herd_tools.py`; no merge listener forwards `blackboard_write` events. So the scorecard cannot tell a killed worker's stub from silence, and the executor writes one on every interrupt that finds no claim (`executor.py:339-341,302`, as #17 requirement 3 asks, though D77 only asks for `kill -9`). Decide whether stubs should be recorded, and tag them so TB§11.5's dead-end re-hit metric (`testbed/scorecard/metrics.py:169`) can exclude them if they are.

## Lower severity

- Commit messages can forge `Herd-Seq` trailers: `store.py:335-353` takes the first trailer line anywhere in the body, and `head_sequence` jumps on reopen. Not reachable through the tool server today. Parse with `git interpret-trailers` or scan from the end.
- Workers can cite `private://` keys as evidence and probe which exist, in the blackboard (`blackboard.py:267-268`) and through `surprise` (`herd_tools.py:410`). Content never leaks.
- `claims/<a>/free.txt`, `claims/<a>/sub/x.md` and `events/<a>/sub/x.json` merge unchecked; `.git` segments and `\n` pass `may_create` and die in git as exceptions after `commit_count` was bumped.
- `open_blackboard` passes `objects=None` (`layout.py:52`), so `object://` evidence is always dangling on a git blackboard opened by the mandated factory; `init_layout` writes as `human`, so a pack that ships `map/gap.md` (integrator-only in `WRITE_RULES`) will make it raise.
- The Redis adapter's `scan` does not escape glob characters (`redis/store.py:116`); the reclaim marker key collides for two acquisitions at the same fake-clock instant.
- Gate dead ends are written `proposed` by `eval_service` and nothing ever confirms them, so `similar_dead_ends` (confirmed only, D26) will never warn about a gate failure unless W1-02 special-cases them.
- Recorder `refs` come only from literal `candidates/<hash>/` pointers (`herd_tools.py:86-99`); a dead end citing the result claim by id, the pattern the toy worker uses, is refused by the tool server although the blackboard accepts it. `/hook/ack` accepts a foreign interrupt id and returns 500 on a bad body before the token check. `claims_written` loses claims written without a lease and on restart.
- `tick` runs post-result gates before checking `constraint_ok`; a remeasure overwrites `jobs_by_hash` so later duplicates point at the Integrator's claim; a stranger's attempt with someone's confirm token destroys it.

## Tests that cannot fail

- **L0-1's slot-gap assertion** is satisfied by FIFO: the task with the larger gap is also enqueued first (`tests/l0/test_l0_01.py:49-65`). Removing the gap term from the implementation still passes.
- **L0-13's `n_reps`** can only ever be 1 (S7).
- **L0-7** covers the pre-submit gate only (B5).
- **The 50 ms hook test** (`tests/unit/tools/test_server.py:118-133`) measures a median on an idle server against a dict router; S1 shows the real number under contention. Assert the maximum while another agent holds the lock.
- **The external-pool test** uses two stores (B7). **The concurrency test** in `test_herd_tools.py:180` puts its barrier in the one place the lock is released.
- **`check_versioned_store`** cannot see tree semantics (B2), and the mirror and dangling-evidence tests run on the memory store only.
- No test has a raising subscriber (S3), a concurrent reader (S4), concurrent reclaimers (L0-8 runs them sequentially), or `n_for_ci > 1` for the noisy promotion rule.

## Process notes

- `main` in this checkout was stale (#39 merged on origin); the review used `origin/main`.
- The executor files were committed as `wip executor` on `w1-07-executor` by another session while this review ran, in the same checkout.
- The real smoke variant (`tests/fixtures/herd.py:206-240`) does not include the tool server, so B1, S1, S2, S9 and S10 are invisible to it.
- The one structural theme: three components (blackboard, queue, eval) rely on in-memory state that the blackboard could rebuild, and each has a path where a transient error leaves that state inconsistent with the repo. Rebuild-from-repo is D13's promise and none of the three tests it.
