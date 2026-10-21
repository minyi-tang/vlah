# Python SDK

> [!NOTE]
> **Preview.** VLAH is pre-alpha. This page describes the interface we are building; names and signatures may still change. If your setup doesn't fit, [open an issue](https://github.com/minyi-tang/vlah/issues/new).

The CLI covers the common path; the [quickstart](quickstart.md) walks through it. Use the SDK when you want to:
- drive a herd from your own code;
- plug in your own judge, agent harness or infrastructure;
- test an adapter you wrote.

## Contents

- [Concepts](#concepts)
- [Run a herd from Python](#run-a-herd-from-python)
- [Plug in your judge](#plug-in-your-judge)
- [Plug in your agent harness](#plug-in-your-agent-harness)
- [Run on your infrastructure](#run-on-your-infrastructure)
- [Test your adapter](#test-your-adapter)
- [Events](#events)

## Concepts

| Term | Meaning |
| --- | --- |
| **Problem folder** | Everything about one problem: the charter, the directions, the judge descriptions and the deployment config. |
| **Charter** | What you and the lead agent agreed: the problem, what counts as progress, constraints and things to avoid. The herd starts only when the charter is signed. |
| **Direction** | One way of attacking the problem, such as "make tool definitions deterministic". Workers are spread across directions. |
| **Worker** | One agent process. It attacks the whole problem from its direction, with access to all the material. |
| **Blackboard** | The shared, append-only record of everything workers have learned. |
| **Claim** | One entry on the blackboard: a result, an insight, a dead end or an open question. |
| **Candidate** | A change a worker wants scored: a patch, a config or an artifact. |
| **Judge** | Anything that scores a candidate. VLAH calls this a *signal source*. Judges are arranged from cheap to expensive, and a candidate must pass the cheap ones first. |
| **Baseline** | The current best combination. The integrator stacks verified wins onto it one at a time, and keeps each only if it still helps. |
| **Lead agent** | The agent you talk to. It agrees the charter with you and brings the big decisions back to you. |

## Run a herd from Python

```python
import vlah

herd = vlah.Herd.from_config("cache-hit/herd.yaml")   # or build it in code, below
run = herd.start(workers=8)

for event in run.events():
    match event.type:
        case "baseline_advanced":
            print(f"baseline {event.baseline}: {event.score:+.3f}")
        case "escalation":
            print(event.question, event.options, "default:", event.default)
            run.answer(event.id, event.default)

report = run.report()
report.patch.write("cache-hit.patch")
print(report.attribution)   # each improvement's gain on the baseline it was merged onto
```

`Run` also has:
- `status()`, for directions, quota used and the current baseline;
- `search(query)`, to search the blackboard;
- `stop()`, which stops cleanly and keeps everything learned.

A run can be resumed: `vlah.Herd.attach("cache-hit/")` reconnects to a run in progress.

Everything in `herd.yaml` can be set in code instead:

```python
from vlah.adapters import executors, harnesses, humans, models, stores

herd = vlah.Herd(
    problem="cache-hit/",
    executor=executors.Local(),
    harness=harnesses.ClaudeCode(),
    store=stores.Local(".herd/"),
    models=models.Routes(default="<your-strongest-model>"),
    human=humans.CLI(),
    judges={
        "unit_tests": vlah.judges.Command("pytest -q tests/"),
        "harness_bench": vlah.judges.Command("private/run_bench.sh"),
    },
)
```

## Plug in your judge

Every judge has two parts:

1. **A description** in `eval/sources/<id>.yaml`, which workers can read. It says how slow the judge is, how many runs it allows and how far to trust it. The required fields are listed in the [quickstart](quickstart.md#4-connect-your-judges). VLAH works out from these what order to run the judges in, and which judge's results may change the budget.
2. **An adapter**, which workers never see, that actually runs the judge.

A shell command that prints `{"score": …}` is enough for most judges: use `vlah.judges.Command`. For a service, a query or a person, subclass `SignalSource`:

```python
from vlah import SignalSource, Candidate, Score

class HarnessBench(SignalSource):
    """Pull mode: VLAH submits a candidate and polls for the result."""

    def submit(self, candidate: Candidate) -> str:
        # candidate.checkout() gives a working tree with the change applied
        return bench_client.start(repo=candidate.checkout(), traces="heldout")

    def poll(self, job_id: str) -> Score | None:
        job = bench_client.get(job_id)
        if not job.done:
            return None
        return Score(value=job.hit_rate, n=job.repeats, ci=job.ci95)
```

For **push** judges, results arrive on their own schedule, for example a dashboard query or a live experiment. Call `self.emit(Score(...), exposure=tag)` from a callback instead of implementing `poll`. Workers register a candidate's exposure first, so every signal can be credited to the right candidate.

You never write quota, deduplication or ordering logic in an adapter. The eval service handles all three, and every score comes back with `n` so noise is always visible.

## Plug in your agent harness

Workers talk to the herd through an MCP tool server. Any harness that can call MCP tools can be a worker without code changes:

| Tool | What it does |
| --- | --- |
| `repo_read`, `search` | read and search the whole blackboard |
| `repo_write` | add claims; workers can only add, never edit |
| `eval_request` | ask for a judge run; the worker must include a predicted score and a reason |
| `surprise` | record something unexpected without needing a conclusion |
| `fork` | save a checkpoint and start a sibling worker |
| `ask_checkpoint` | ask a question of a past checkpoint, including the lead agent's alignment conversation |

To add a harness, tell VLAH how to launch it and what it supports:

```python
from vlah import Harness, AgentSpec

class MyLoop(Harness):
    supports_tool_hooks = False   # True if VLAH can see every tool call, not just its own tools
    supports_checkpoint = False   # True if a conversation can be exported and resumed

    def build(self, task, ctx, tool_endpoint, hooks) -> AgentSpec:
        return AgentSpec(
            command=["python", "-m", "my_agent", "--mcp", tool_endpoint.url],
            files={"CONTEXT.md": ctx.render()},
            env={"GOAL": task.goal},
        )
```

Both capabilities are optional, and missing ones degrade gracefully:
- Without tool hooks, a worker learns about relevant new results only when it calls a VLAH tool, rather than on every tool call.
- Without checkpoints, `fork` restarts from the worker's trace instead of copying its conversation.

## Run on your infrastructure

The herd touches the outside world only through six ports. Choose an adapter for each one in `herd.yaml`:

```yaml
executor: {kind: kubernetes, namespace: herd, image: ghcr.io/you/worker:latest}
harness:  {kind: claude-code}
store:
  versioned: {kind: git, url: "git@github.com:you/cache-hit-herd.git"}
  kv:        {kind: redis, url: "redis://redis:6379"}
  objects:   {kind: s3, bucket: herd-traces}
  vectors:   {kind: sqlite-vec}
models:
  default: <your-strongest-model>
  embed:   <your-embedding-model>
human:    {kind: slack, channel: "#cache-hit-herd"}
judges:
  harness_bench: {class: "my_judges:HarnessBench"}
```

| Port | Interface | Adapters (planned) |
| --- | --- | --- |
| **Executor** | `start(spec, task, ctx)`, `checkpoint(handle)`, `restore(blob)`, `kill(handle)`, heartbeat | `Local`, `Kubernetes`, `Ray` |
| **Harness** | `build(task, ctx, tool_endpoint, hooks)`, plus the two capability flags | `ClaudeCode`, `AgentSDK`, `OpenHands` |
| **SignalSource** | `submit` and `poll`, or `emit` for push judges | `Command`, `HTTP` |
| **Store** | a versioned store, a key-value store, object storage and a vector index | `Local`, `Git`, `Redis`, `S3`, `SqliteVec` |
| **Human** | a message channel to the lead agent | `CLI`, `Slack` |
| **Model** | `complete(role, messages, tools)` and `embed(texts)` | any provider, routed per role |

To write your own adapter, subclass the port in `vlah.ports` and refer to it in `herd.yaml` as `{class: "your_module:YourAdapter"}`.

There are a few rules your adapter must keep:
- **Versioned store:** the order in which entries are merged must only ever increase across the whole herd. The rest of the system relies on it.
- **Executor:** the heartbeat must come from the executor, not from the agent. A worker in the middle of a long sandbox job must not look dead.
- **Model:** every role should get the strongest model you have. VLAH rations judge runs, not tokens.

## Test your adapter

Every port has an in-memory adapter, so a whole herd can run a toy problem in one process in seconds. Use it to test your own adapters in two steps.

**1. The conformance suite** checks that the adapter keeps the port's contract:

```python
# test_my_adapters.py
from vlah.testing import check_executor, check_signal_source
from my_infra import MyExecutor
from my_judges import HarnessBench

def test_executor():
    check_executor(MyExecutor())        # start, kill, checkpoint and restore, heartbeat during long jobs

def test_judge():
    check_signal_source(HarnessBench(), sample="tests/fixtures/candidate.patch")
```

**2. The swap test** runs the demo with your adapter in place of the in-memory one:

```bash
uv run vlah demo --executor my_infra:MyExecutor
```

The demo is seeded, so the deliverable must match the in-memory run. If it doesn't, the adapter is changing behavior that it should only be carrying.

## Events

Everything the herd does is an event in an append-only log. `run.events()` streams them, and `vlah events cache-hit` prints them.

| Event | When |
| --- | --- |
| `claim_written` | a worker records a result, insight, dead end or question |
| `dedup_hit` | a candidate matched one already judged, so no judge run was spent |
| `result_ready` | a judge returned a score |
| `baseline_advanced` | a win was stacked onto the baseline and still helped |
| `integration_failed` | a win stopped helping on the new baseline and was dropped |
| `interaction_found` | two wins overlap, or one only works together with the other |
| `baseline_reverted` | a later check showed a merged win didn't hold up, so it was rolled back |
| `escalation` | a decision is waiting for you; it includes options and a default |
| `plateau` | no improvement for a while; you choose whether to continue, revise the charter or stop |
| `stopped` | the run ended: budget spent, target reached, or stopped by you |
