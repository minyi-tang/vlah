# Quickstart

> [!NOTE]
> **Preview.** VLAH is pre-alpha, so the commands on this page describe the interface we are building and don't run yet. If something here wouldn't work for your problem, [open an issue](https://github.com/minyi-tang/vlah/issues/new). Changing it now is cheap.

This guide takes you from a fresh clone to a combined patch for your own problem. The running example is raising the prompt-cache hit rate of an agent harness.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- git
- Nothing else for the demo. For a real run you also need:
  - an API key for your model provider;
  - the agent harness your workers will use, for example the Claude Code CLI, logged in.

## 1. Install

```bash
git clone https://github.com/minyi-tang/vlah && cd vlah
uv sync
uv run vlah --version
```

## 2. Run the demo

```bash
uv run vlah demo
```

The demo runs a whole herd on a toy problem in one process, using in-memory adapters, scripted workers and a fake judge. It takes about 10 seconds and needs no API keys, and it is the same run we use as a regression test. You should see directions being explored, duplicates skipped, and wins stacking onto the baseline:

```console
demo  toy-scheduler · 4 workers · 3 directions · judge quota 6

  [00:01] w1  candidate c-3f2  queued for judge (predicted +2.0)
  [00:02] w3  candidate c-9a0  dedup_hit (same as c-3f2)
  [00:04] judge c-3f2  +2.4 ± 0.3
  [00:04] baseline_advanced  b1 = b0 + c-3f2
  [00:06] w2  dead_end  "batching by deadline": no gain
  ...
  baseline 100.0 -> 108.1 · judge 6 of 6 used · 2 duplicates skipped
```

## 3. Create a problem folder

```bash
uv run vlah init cache-hit --repo ~/code/my-harness
```

```
cache-hit/
  charter/                 what you and the lead agent agree on
    problem.md             the problem, in your words
    progress.md            what counts as progress, and what is just noise
    constraints.md         judge quota, deadline, things that must not change
    params.yaml            machine-readable knobs (defaults are fine)
    status.md              draft | aligned | revising; the herd starts only when aligned
  approaches/              directions to explore (the lead agent can propose these)
  eval/sources/            what each judge is, for example how slow, how scarce and how much to trust it
  herd.yaml                how to run each judge, where workers run, which models to use
  private/                 judge scripts and hidden data
```

Workers see everything except `herd.yaml` and `private/`. That keeps the evaluation honest: a worker can't tune its changes to the scoring script.

## 4. Connect your judges

A **judge** is anything that scores a candidate: a unit-test suite, a benchmark, a query, or a person. Most problems have a cheap judge and an expensive one. VLAH arranges them as a ladder: a candidate must pass the cheap judges before it can spend a run on an expensive one.

Describe each judge in `eval/sources/`. These files are public: workers read them to plan their work.

```yaml
# eval/sources/unit_tests.yaml
id: unit_tests
mode: pull                 # VLAH submits candidates (push: results arrive on their own)
latency: 2m
budget: {per_day: 2000, allocated_by: planner}
cost_machine_hours: 0.05
trust: 0.3                 # 0 to 1; only the most trusted judge can move the budget
readable_by: [worker, critic]
```

```yaml
# eval/sources/harness_bench.yaml
id: harness_bench
mode: pull
latency: 30m
budget: {per_day: 48, allocated_by: human}
cost_machine_hours: 1
trust: 0.9
readable_by: [critic]      # workers can't see how it is computed
noise: {n_for_ci: 3}       # repeat runs needed for a confidence interval
```

Then tell VLAH how to run each judge, in `herd.yaml`:

```yaml
# herd.yaml (never shown to workers)
judges:
  unit_tests:    {command: "pytest -q tests/"}
  harness_bench: {command: "private/run_bench.sh"}
```

A command judge runs in a checkout of the candidate. It must print one JSON line, `{"score": 0.634}`, or `{"score": 0.634, "n": 3, "ci": 0.01}` if it repeats internally. For a judge that is a service, a query or a person, write a small adapter class instead; see [Plug in your judge](sdk.md#plug-in-your-judge).

## 5. Agree the charter

```bash
uv run vlah chat cache-hit
```

You describe the problem to the **lead agent**, which then:

1. repeats it back in its own words;
2. lists what it assumed but you didn't say;
3. proposes three to five directions, each with a prediction of where it will be after a day and after a week.

You correct it until you agree, then sign. Signing sets `charter/status.md` to `aligned`.

If you'd rather write the charter yourself, edit the files in `charter/` and `approaches/` directly, then:

```bash
uv run vlah sign cache-hit
```

## 6. Run the herd

```bash
uv run vlah run cache-hit --workers 8
```

By default, workers run as local processes and state lives in a local git repository under `cache-hit/.herd/`. To run on Kubernetes, use Redis and S3, or route roles to different models, change `herd.yaml`; see [Run on your infrastructure](sdk.md#run-on-your-infrastructure).

While it runs:

```bash
uv run vlah status cache-hit     # directions, judge quota used, current baseline
uv run vlah inbox cache-hit      # questions waiting for you; answer with: vlah answer <id> <choice>
uv run vlah stop cache-hit       # stop cleanly; everything learned so far is kept
```

The herd sends you a question only for big decisions, such as dropping a direction that holds a large share of the budget. Each question comes with options and a default that is applied if you don't answer in time (8 hours unless you change it). The default is always the safe choice: pause the affected direction rather than kill or continue it.

## 7. Read the result

```bash
uv run vlah report cache-hit
git -C ~/code/my-harness apply "$PWD/cache-hit/report/combined.patch"
```

The `report/` folder holds:

| File | What it holds |
| --- | --- |
| `combined.patch` | every improvement that still helped when stacked, as one patch |
| `attribution.md` | each improvement's measured gain on the baseline it was merged onto |
| `merge-log.md` | every merge attempt, including the ones that were dropped and why |

Dead ends, insights and full worker traces stay on the blackboard under `cache-hit/.herd/`. You can search them with `vlah search cache-hit "<query>"`.

## Next

- [Python SDK](sdk.md): drive a herd from code, plug in your own judge, agent or infrastructure, and test your adapters.
- [Design overview](design.md): why the system is built this way and how we will check that it works.
