# The status file

A running herd keeps its leases, its task queue and its worker slots in memory. To let anything outside the process see them, such as `vlah status` or the observatory UI (#111), the run writes `<problem>/.herd/runs/<run id>/status.json`.

### When it is written

- after every supervisor tick (every 5 seconds in `vlah run`, `poll_seconds` in the file);
- when the run starts closing, and once the closeout has finished.

Each write goes to a temporary file in the same directory, which is then renamed over `status.json`. A reader therefore sees either the previous status or the new one, never a partly written file. A file whose `written_ts` stops moving while `phase` is `running` belongs to a process that has died.

### Versions

[status-schema.json](status-schema.json) is the JSON Schema of the current version. A test generates the schema from the code (`vlah.runtime.status.RunStatusFile`) and checks that it matches this file. Any change to the fields, including an added field, bumps `schema_version`, so a reader can check the version and then rely on every field. The current version is 1.

### Fields

Timestamps (`*_ts`) are seconds since the epoch on the run's clock, and durations end in `_seconds`.

| Field | What it holds |
| --- | --- |
| `schema_version` | 1 |
| `run_id`, `arm` | The run and its arm (`B0`, `B1`, `S1` or `S2`) |
| `phase` | `running`; `closing` from the first stop request until the closeout ends; `finished` after it |
| `written_ts`, `started_ts`, `deadline_ts` | When this status was written, when the run started, and when its time limit ends it |
| `poll_seconds` | Time between supervisor ticks |
| `stop_reason` | Why the run is stopping, such as `vlah stop`, `the run's time limit` or a stop rule; `null` while it runs |
| `closeout_error` | Set when the Integrator's closeout failed |
| `deliverable_baseline` | The deliverable's baseline number, once the closeout has run |
| `head_sequence` | The last event sequence number on the blackboard |
| `workers` | One entry per worker slot, the compose slot included (`is_compose`) |
| `workers[].task` | The task the worker holds a lease on (`id`, `kind`, `approach`, `goal`, `priority`, `refs`), or `null` when the worker is idle |
| `workers[].lease_age_seconds`, `last_heartbeat_ts`, `files_touched` | From the lease. A new session moves the lease, so its age is the current session's age |
| `workers[].session_count` | Sessions the worker has started in this process |
| `workers[].crash_count` | Times the supervisor restarted the worker's loop |
| `workers[].last_write_ts` | When a write by this worker last merged on the blackboard |
| `queues.arm_waiting`, `queues.compose_waiting` | Tasks waiting in the arm queue and in the compose queue |
| `judge.runs_used` | Judge runs the judges made, from each candidate's `result.json`. A cache hit is not a run |
| `judge.machine_hours_used`, `machine_hours_limit` | The budget meter's total and the charter's limit |
| `judge.sources[]` | Per judge: `runs_used`, `run_cap` (the charter's run limit, or `null`), `per_day_cap` (its daily quota for worker runs) and `machine_hours_used` |
| `baseline` | The Integrator's current baseline: `k`, `score` (the mean of its per-seed scores, `null` when unmeasured), `candidate_count` and `guard_pending` |
| `recent_events` | The last 20 events merged since this process started, as the blackboard stores them, with `seq` set |

### Asking a run to stop

`vlah stop <problem>` writes `stop-request.json` in the same directory. The supervisor looks for it on every tick. When it finds the file, it removes it and stops the run as `stop()` does: the workers are killed, the Integrator closes out once, and a `stopped` event with reason `human` is written. The supervisor ignores the file's content. A run that starts removes a request left by an earlier process.
