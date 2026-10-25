# Quickstart

> [!NOTE]
> **Pre-alpha.** Installation and the deterministic demo work today. The later problem-setup and real-run examples include planned interfaces, such as `vlah chat`. If something here wouldn't work for your problem, [open an issue](https://github.com/minyi-tang/vlah/issues/new).

This guide takes you from a fresh clone to a combined patch for your own problem. The running example is raising the prompt-cache hit rate of an agent harness.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- git
- Nothing else for the demo. For a real run you also need:
  - an API key for your model provider;
  - the agent harness your workers will use, for example the Claude Code CLI, logged in.

## 1. Install

### From source

The uv workspace installs VLAH and its testbed dependency editably, so local edits
remain visible:

```bash
git clone https://github.com/minyi-tang/vlah && cd vlah
uv sync --locked
uv run vlah --version
```

### From built artifacts

No PyPI publication is assumed. From a checkout, build both distributions, then
install only the VLAH wheel into an environment outside the repository:

```bash
uv build --all-packages --no-sources
artifacts="$PWD/dist"
install_dir="$(mktemp -d)"
uv venv "$install_dir/.venv" --python 3.11
uv pip install --python "$install_dir/.venv/bin/python" \
  --find-links "$artifacts" "$artifacts/vlah-0.0.1-py3-none-any.whl"
cd "$install_dir"
.venv/bin/vlah --version
.venv/bin/vlah --help
.venv/bin/vlah demo --seed 1
```

Keep both wheels in the artifact directory. The installer resolves
`vlah-testbed>=0.1.0,<0.2.0` from that directory and fetches the other declared
dependencies from its configured package index. VLAH remains at `0.0.1`; the
testbed has its own version, `0.1.0`. Neither package needs `PYTHONPATH` or an
editable installation for these commands.

VLAH's source archive, `vlah-0.0.1.tar.gz`, can replace its wheel in the install
command. It includes the VLAH package, worker prompts, router rules, type marker,
README, license, VCS ignore file and build metadata, and rebuilds without the
repository. The testbed remains a separate declared dependency; its code, problem catalog, and
the repository's tests, UI, examples, docs and private state are not copied into
VLAH's archive.

The remaining examples use the source workflow's `uv run vlah`. With an artifact
installation, use the installed environment's `vlah` command instead. A real run
still needs Git, the configured model CLI and credentials, and a problem folder;
installing the Python packages does not provision them.

## 2. Run the demo

```bash
uv run vlah demo
```

The demo runs a whole herd on a toy problem in one process, using in-memory adapters, scripted workers and a fake judge on a simulated clock. It takes about a second and needs no API keys, and the same seed always gives the same run, which a regression test checks (`--seed` picks another). You should see directions being explored, a duplicate skipped, an idea that does not help, and wins stacking onto the baseline:

```console
demo  toy-scheduler · 4 workers · 3 directions · seed 0

  [00:30] result_ready       c-eval_service-00001 c4f3dc6a48a8 by worker-01 at toy_judge: score 102.3, gain +2.3 ± 0.12
  [00:30] result_ready       c-eval_service-00002 929a18ea8d60 by worker-02 at toy_judge: score 99.28, gain -0.725 ± 0.1
  ...
  [00:30] baseline_advanced  baseline@1 = baseline@0 + c4f3dc6a48a8, gain +2.3 ± 0.12
  [00:30] dedup_hit          01d12d63a27c repeats c-eval_service-00003
  [00:50] integration_failed 929a18ea8d60 on baseline@1: below_1se
  ...
  [03:50] baseline_advanced  baseline@4 = baseline@3 + 726be375291b, gain +1.49 ± 0.071
  [04:00] escalation         spend_over_fraction  10.00 of 12.00 eval machine-hours spent (83%). ...
  [08:00] stopped            time: the run's time limit

  baseline 100 -> 108.7 (baseline@4) · 11 judge runs · 1 duplicate(s) skipped · 1 dead end(s)
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
uv run vlah events cache-hit --follow   # every event as it merges
uv run vlah inbox cache-hit      # questions waiting for you; answer with: vlah answer <id> <choice>
uv run vlah stop cache-hit       # stop cleanly; everything learned so far is kept
```

Each command takes `--run-id` (by default, the run that changed last), and all but `stop` take `--json`. The run writes its live state to `cache-hit/.herd/runs/<run id>/status.json`, which [the status file](status-file.md) describes.

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
