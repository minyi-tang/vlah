# Improve the scheduling policy

Six data centers run the same scheduling policy against different public
workloads. Deliver one policy that lowers the average task waiting time across
the six sites without worsening any site's p99 waiting time under the official
service's constraint rule. The score is the negative average of the six sites'
mean waits in seconds: higher is better.

## What you can change

Your candidate is `target/policy/`: `policy.py` plus `params.yaml`, with an
optional top-level `manifest.yaml` recording its baseline target. Do not submit
the enclosing git checkout. Preserve the candidate's baseline identity when
copying or rebasing it.

The packed `problem/sim/policy_api.py` defines the interface. The four hooks are:

| Hook | Responsibility |
| --- | --- |
| `order(queue, state)` | Return a permutation of the queued job IDs |
| `admit(job, state)` | Decide whether to consider starting this job on this pass |
| `place(job, free, state)` | Choose a machine with sufficient free cores, or return `None` |
| `preempt(running, state)` | Choose running jobs to kill and requeue from scratch |

Missing hooks use the baseline implementation. Read the public interface's
execution and validity limits before changing a hook. A locally attractive
schedule is not useful if it violates the interface, exceeds the hook budget,
starves jobs, or fails a site's tail-latency constraint.

`problem/` contains packed public simulator code, site observations, public
traces, baseline scores and the opportunity-map script. Use that material and
the supplied tools only. Do not look for the source repository, private traces,
judge definitions or operator credentials.

## How to gather evidence

The supported fast tier is the service-backed command:

```bash
tb fast target/policy --seed 1 --out fast.json
python problem/map/gap.py fast.json --out gap.md
```

Public seeds 1 through 5 are allowed. Fast runs are free, unlimited and noisy;
compare a candidate with the baseline on the same seed and inspect per-site and
per-class results. The map separates waits by site, job size, estimated duration
and user. A simulator run over packed traces is only a smoke check: it does not
include the service's machine behavior and cannot replace `tb fast`. If the
service cannot be reached, report the failure rather than treating local output
as service evidence.

For formal scoring, submit through the herd's `eval_request` tool with the
candidate directory, a predicted score, and a note explaining the expected
change. Include fast-tier evidence and preserve parent/baseline lineage. Do not
call `tb submit` to bypass the herd Eval client or spend audit/verdict quota.
The client and service handle quota, duplicate detection and waiting for results.

The default slow tier allows the whole herd 2 submissions per hour and 96 in
total. A result uses 20 seeds not available to workers. It stays hidden until
both 600 seconds have elapsed since submission and computation has finished.
Only official slow-tier results count as verified evidence; retain their sample
counts, uncertainty and `constraint_ok`, not just a best score.

## Initial directions and delivery

Start with the four public policy hooks, one slot each: queue ordering,
admission, placement and preemption. The approach files describe questions to
test, not established explanations of the site symptoms. Establish comparable
measurements before claiming that a direction works.

Before each scarce evaluation, state a prediction. After its result, compare the
prediction with the measured change, record counterevidence and avoid repeating
known dead ends. Keep claims and decisions in the herd's append-only record.
Retain restart notes and pending ticket references rather than relying on session
memory.

Keep changes small enough to combine. Rebase onto the baseline assigned by the
herd and resolve interactions using evidence. The Integrator owns the delivered
baseline; neither the highest single noisy score nor a list of unrelated
candidates is the deliverable. Do not declare a separate final through `tb final`.

The charter is a draft until the operator signs it. This brief does not authorize
a launch, change source permissions, allocate ten workers, or preregister a
comparison.
