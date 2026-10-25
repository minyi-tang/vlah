# Running the herd on the testbed

`vlah arm run` is the entry point called by `tb run --arm herd`. The testbed
runner creates the service run, packs the problem, supplies the shared
`IntegratorConfig`, and keeps `TB_SYSTEM_KEY` in the operator process.

Install both workspace packages first with `uv sync --locked --all-packages`.
This supplies the `vlah` and `tb` commands and the slow-eval app's dependencies.

```bash
VLAH_TESTBED_TEMPLATE=/path/to/approved-scheduler-template \
  tb run --arm herd --problem sched --workers 2 --hours 0.1
```

The template defaults to `examples/testbed-sched/`; `vlah arm run --template`
overrides it directly. The checked-in template is deliberately a draft. An
operator must approve its source access policy and accounting values and sign a
deployment copy before a run. Conversion never signs a charter, chooses a model,
changes a quota, or copies the source problem's hidden material.

## Files and credentials

Conversion writes `herd-problem/` beside the packed problem, refusing an existing
destination. Its `target/` is a new repository containing only the packed
`policy/` directory. Workers edit and submit `target/policy/`, not `target/`:
the candidate hash must not include `.git`. Each worker also gets `problem/`,
the public pack, and `BASELINE_TARGET`. New sessions start from `baseline@0`;
workers can use the blackboard's published baselines and record the baseline
they actually build on in their candidate manifest.

The assembly registers `worker-01` through `worker-NN` and the separate compose
slot, `worker-00`, before starting any worker. Tokens are stable HMACs over the
service key, run id and actor. Workers receive only their own token and the
service URL, run id, actor, problem directory and target-file path. Registration
must grant only the fast-tier capability; ordinary testbed tokens are not a
substitute. Official submissions go through the herd's Eval client. The
compose slot's trusted slow submissions use the service's `composer` identity
and Integrator quota.

The macOS sandbox enforces the egress allowlist and denies file contents outside
the worker's own directory, an isolated runtime-only Python copy, OS/Python
runtimes and explicitly configured executable dependencies. `HOME` and `TMPDIR`
are private to the worker. Operator files, source checkout, other workers,
service key, calibration, and testbed operator modules are not exposed.
Unsupported platforms fail closed; there is no unsafe testbed launch flag.

Extra runtime dependencies can be named by
`herd.yaml` `executor.settings.sandbox_read_paths`. These are paths to installed
executables or libraries, not repository roots or credential directories.
Repository-capable GitHub credentials and `TB_SYSTEM_KEY` are rejected in harness
settings. Use the configured model gateway rather than handing repository
credentials to a worker.

## Assembly API

The launch path is independent of any observer:

Harness construction uses the shared local factory, including its source IDs
and scripted harness support. A scripted harness uses its configured
`python_executable` (or the runtime's Python), not a model CLI.

1. `convert_testbed_problem(packed_root, template_root, destination, clock=clock)`
   returns a loaded `Problem`. It preserves the template's parameters, sources,
   limits and approvals, but excludes source definitions from the blackboard.
2. Construct `TestbedAssembly(client=SlowTierClient(base_url=..., run_id=...,
   system_key=...), integrator_config=IntegratorConfig.from_json(...),
   worker_count=...)`.
3. Pass it, or an external `Assembly` wrapper, to `build_herd` with the same
   `run_id`, `worker_count`, injected clock/random generator, and run root.
   The usual `RunComponents.executor` and blackboard hooks remain available.
4. Start the herd, wait for it to stop, and call
   `assembly.finalize(run.report())`. Always call `assembly.close()` to stop its
   servers, executor and store. Stop a still-running herd before closing resources.

The CLI writes `run.json`, `status.json`, `report.json` and the service's final
receipt (`final.json`) below `herd-problem/.herd/runs/<run id>/`. It does not
implement testbed resume.

## Calibration and closeout

The assembly reads the daily and audit baseline scores and both noise estimates
from `/calibration`; reference scores and ingredient labels are not retained.
The supplied configuration must equal `config_from_params(problem.params)` in
every field. The shared merger keeps its entire configured audit budget; this
assembly does not allocate an additional Critic recheck share or score a
separate verdict. The testbed runner owns the final check and scorecard.

Herd tools send recorder events through `SlowTierRecorder`, and a blackboard
subscriber forwards merger events. After the Integrator freezes its measured
deliverable, `finalize` calls `/final` as `runner` with that exact hash. It does
not pick a best worker candidate or fall back to another arm.
