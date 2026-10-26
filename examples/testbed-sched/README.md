# Scheduler problem for the herd

This is the **unsigned draft** herd-side folder for testbed problem A. It supplies
the brief, four initial approaches, source declaration and charter for the testbed
bridge in [#122](https://github.com/minyi-tang/vlah/issues/122). It is not a worker
pack, a running deployment, a signed charter or a preregistration.

Check that it loads without launching a worker or contacting a judge:

```bash
uv run python -c 'from pathlib import Path; from vlah.core.problem.loader import load_problem; print(load_problem(Path("examples/testbed-sched")).status)'
```

The result must be `draft`. `load_problem` checks schemas, not human approval.
Leave the checked-in `charter/status.md` unchanged; review and configure a separate
operator copy before signing it. A local UI-feedback session is not a pilot or an
arm comparison and does not confer that approval.

`uv run pytest tests/unit/cli` also works with a core-only `uv sync --locked`
installation. Its quota check reads the public settings as a literal without
importing the scheduler simulator or requiring testbed dependencies. CI installs
the full workspace with `uv sync --locked --all-packages`.

## Public inputs

The brief uses only the public [PI prompt](../../problems/sched/pi_prompt.md),
[tool instructions](../../problems/sched/tools.md),
[policy interface](../../problems/sched/sim/policy_api.py), and
[opportunity map](../../problems/sched/map/gap.py). The site observations in
`map/gap.md` are the `summary` fields included in the packed worker view, not
workload or machine-behavior settings.

The public interface names four hooks: `order`, `admit`, `place`, and `preempt`.
Each has one draft approach and one slot. These are hypotheses to investigate,
not a list of planted mechanisms or known solutions. No improvement is claimed
and no measurement has been run to write this folder.

Do not copy `problems/sched/` wholesale into a worker sandbox. The bridge must
obtain worker material through `tb pack-problem` and use only that packed copy.
In particular, the packed simulator is suitable for local smoke checks, but it
does not reproduce the service-side machine behavior. Workers use the
authenticated **service-side** `tb fast` path for fast-tier evidence.

## Bridge interface

This directory itself is the `load_problem` root. Its public snapshot contains
the charter, brief, approaches, source declaration and opportunity map.
`README.md`, `herd.yaml`, any `private/` directory and run state are not public
inputs. This template contains no private material.

The bridge consumes an operator-reviewed copy through `--template`. It refuses
an unsigned template and generates a separate problem folder and target
repository; it must not sign this draft or infer missing source policy.

| Surface | Contract |
| --- | --- |
| Slow source | `slow`, declared in `eval/sources/slow.yaml` |
| Judge | `vlah.adapters.signal.testbed_http.client:SlowTierSource` |
| Candidate in the worker sandbox | `target/policy/`, containing `policy.py` and `params.yaml`; its optional top-level `manifest.yaml` carries the baseline target |
| Public worker assets | `problem/`, from the packed copy only; the map command is `python problem/map/gap.py fast.json` |
| Target repository | The bridge replaces `__TARGET_REPOSITORY__` in `herd.yaml` with a fresh repository made from the packed policy |
| Service configuration | The bridge supplies the URL, run ID, private service credentials, worker-scoped fast access and service calibration; none belongs in the public snapshot |
| Baseline | The bridge seeds `manifest.target` with `baseline@0` and maintains `TB_TARGET_FILE`/`BASELINE_TARGET`; workers retain the target associated with the candidate |

Do not submit `target/`: it is a git checkout, not the candidate payload. The
service evaluates the two policy files, not the source repository or simulator.
Only the herd Eval client submits to the slow tier; workers use `eval_request`,
not direct `tb submit`. There is no metered fast source in the herd ladder:
`tb fast` is a free, single-public-seed screening tool.

## Quotas and accounting

The declared defaults match the public `DEFAULT_KNOBS` in
`problems/sched/problem.py`:

| Limit | Default |
| --- | --- |
| Worker slow submissions | 2 per fixed hour window, 96 total |
| Visible delay | At least 600 seconds, and until computation finishes |
| Samples per slow result | 20 seeds; no private seed values are stored here |
| Daily declaration | 48, the daily equivalent of 2 per hour |
| Charter duration ceiling | 2 days, the default 48-hour quota envelope |
| Integrator remeasures | 16 total, 1 per hour, separate from the worker service quota |
| Audit / sealed verdict caps | 10 / 5, respectively |

`SourceDecl` has no hourly-window field. Its `budget.per_day` does **not** replace
the service's hourly limit or result-visibility delay. The service remains
authoritative. `noise.n_for_ci` records the 20-sample declaration. The charter's
generic `runs.slow` cap counts source runs, including remeasures; it can stop the
herd before all 96 worker submissions have been used.

The public knobs give no measured or billed machine-hour cost. The numeric
`cost_machine_hours: 1` and `eval_machine_hours: 1` are **schema-only draft
sentinels**, not estimates or approved budgets. Do not turn the artificial
600-second visibility delay into a compute-cost estimate. Both values must be
replaced with operator-approved accounting before signing a deployment copy.

The Integrator parameters match the shared testbed merger: the default
one-standard-error acceptance rule, interaction multiplier 2, an audit every
2 merges, audit multiplier 2.5, and 3 audit runs reserved for bisection/closeout.
No additional expensive gate is invented; the service reports `constraint_ok`.

## Operator decisions still required

[docs/open-questions.md](../../docs/open-questions.md) still leaves source policy
and API limits unanswered. These are not inferred from the prompt-optimization
example.

| Field or decision | Checked-in state | Required before execution |
| --- | --- | --- |
| `eval/sources/slow.yaml: trust` | `0`, draft sentinel | Maintainer-approved trust |
| `eval/sources/slow.yaml: readable_by` | `[]`, no declared definition readers | Maintainer-approved role list and isolation arrangement |
| `eval/sources/slow.yaml: cost_machine_hours` | `1`, draft sentinel | Measured/billed accounting per slow run |
| `charter/constraints.md: eval_machine_hours` | `1`, draft sentinel | Approved run budget using the same accounting |
| `charter/status.md` and `charter/sources.md` | `draft`, unchecked | Review of the brief, approaches, source policy and limits, then an operator signature in the deployment copy |
| Model API concurrency/rate limits | Not set | Operator confirmation that the requested worker count is supported |
| Runtime deployment | No service address, run ID, credentials or real target path | Bridge/operator configuration with worker-safe credential isolation |

The sentinels are not authorization. The unsigned status prevents normal
execution; merely passing the loader does not clear any row above. The four
one-slot approaches are the issue's starting allocation, not a preregistered
worker count. Any feedback-run duration or allocation change must be explicit in
the operator copy/run request. A later comparison additionally needs the
maintainer's preregistration.

## Harness and gateway

`herd.yaml` reuses the existing Copilot harness and `models.worker: gpt-6.1-sol`
from [prompt optimization](../prompt_optimization/README.md). It does not choose
another model, add API limits, or introduce a weaker model for a role.

For an already configured gateway, use the command/environment settings of that
deployment. The linked example also documents the Claude Code gateway wrapper:
its command goes in `harness.settings.command`, its gateway URL in
`harness.settings.model_api_urls`, with `harness.kind: claude_code`. Switching
harnesses or model mappings is an operator choice, not part of this draft.
Keep gateway keys out of public files and do not copy the example's model-graded
judge: the scheduler judge is the testbed HTTP adapter.
