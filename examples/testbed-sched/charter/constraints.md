# Constraints

The default service quota is 2 worker submissions per fixed hour window, 96
total, with 20 seeds per slow result and a minimum visible delay of 600 seconds.
The charter's 2-day ceiling is the corresponding default 48-hour envelope, not a
requirement to run a feedback session for that long. The daily source declaration
is 48; the service also enforces its hourly windows.

```yaml limits
eval_machine_hours: 1
days: 2
runs:
  slow: 96
```

**`eval_machine_hours: 1` is a schema-only draft sentinel.** The matching
`cost_machine_hours: 1` in the source declaration is also a sentinel. Neither is
a compute estimate, a spending approval or a preregistration. An operator must
replace both with approved measured/billed accounting before signing. Artificial
result delay is not machine time.

Integrator remeasures have a separate service quota of 16 total and 1 per hour.
The audit cap is 10, with 3 reserved for bisection and closeout; the sealed verdict
cap is 5. The generic herd source-run cap above includes remeasures, so it may
stop a run before all worker service quota is spent.

Every site's p99 must satisfy the official constraint rule returned by the
service. The aggregate score cannot excuse a failing site. Respect the hook
interface and validity limits in the packed policy API. Do not change the judge,
the simulator, the traces, or the service's constraints.

Only the Eval client spends slow-tier quota. Fast-tier evidence must come from
the supported service path. Do not replace an unavailable service with local
scores, bypass the quota, or put private data or credentials in public files.
