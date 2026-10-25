# Herd observatory

A dark, continuously zoomable view of VLAH's work: fixed worker identities move
among recorded tasks and contributions while candidate ancestry, evidence reuse,
integration and failures leave a persistent causal graph.

**The included recording runs real VLAH components with scripted in-memory
workers, clock and judge. It is not a production run, a benchmark result, or a
simulation of workers multiplying.** No LLM calls or paid judge runs are made.

## Run

Use Node.js 22.18 or newer:

```bash
cd ui
npm ci
npm run dev
```

The generated recording is committed, so viewing it does not require Python.
Keep the server on loopback; this is not an authenticated remote deployment.

## Explore

- Scroll or pinch to zoom continuously, drag to pan, or select an approach,
  worker or artifact. Existing positions do not rearrange when records arrive.
  Plus/minus zoom, arrow keys pan, and Home/Escape return to the overview.
- White lights are workers, diamonds are candidates, and bright milestones are
  baselines. Historical tasks and claims remain visible. Failed work is retained,
  not deleted or converted into a successful result.
- Click a worker for its observed goal, sessions and contributions. The roster
  and search reach all 100 configured workers and the separate compose slot.
  A new session is not a new worker; an ended session is not a judge verdict.
- Click an artifact for its recorded state, history and causal links. Each link
  names the source field or session observation that establishes it. Multi-parent
  candidates and baseline integration are joins, not forced into a single tree.
- Pause, replay, change speed, or scrub backward. Future artifacts and edges
  disappear on rewind, but worker identities remain. Motion illustrates the
  observations; it does not claim to measure physical motion or percent complete.
- The event trail preserves canonical event types and merge sequence numbers.
  Every event is inspectable, including events without a graph node. Its complete
  canonical envelope and payload are retained, not only the fields used to draw
  the graph. Candidate history exposes recorded result payloads, including sample counts,
  without fabricating scores or counting every contribution as a judge run.
- **Run report** shows captured `RunStatus` snapshots and the complete `Report`,
  including the deliverable, baselines and standing metrics. Snapshots have
  observation times; a later report is not revealed while replaying earlier work.
  Missing holdout results remain `null`, not zero or a simulated verdict.
- Reduced-motion users start paused; camera navigation becomes immediate and
  decorative meteor trails are hidden. Inspection tabs work with arrow keys,
  Home and End. Narrow screens retain search, touch navigation and inspection.

## Where the recording comes from

From the repository root, with its development dependencies installed:

```bash
uv run python -m ui.record_replay
```

This regenerates `src/recording.ts` and `public/recording.json`. The workload
makes scripted choices, but its queue, leases, merged claims, evaluation results,
result follow-ups, candidate validation and Integrator decisions run through the
actual repository implementations. It uses:

- `RunFanout` for merged-event delivery and routing of result follow-ups.
- `KeyValueTaskQueue`, `KeyValueLeases`, `StoreIndexer` and `KeyValueRouter`.
- `LadderEvalService` with an in-memory slow source.
- `HerdIntegrator` and the shared merger, including integration failures,
  compose work, interactions and baseline advances.
- The existing `MemoryExecutor`, `ScriptedHarness` and stepped runtime.

The recording contains 100 worker identities plus compose, repeated sessions
on those identities, multi-parent candidates, failed-integration follow-ups
and accepted baselines. Regeneration is tested for byte-for-byte determinism.
Its synthetic scores have no meaning as an experiment or arm comparison.

## Visual baseline for alternatives

Coordination lives in [issue #111](https://github.com/minyi-tang/vlah/issues/111).
Start an alternative from its published baseline commit, in a separate branch;
do not modify the shared baseline branch. The issue remains the reference for
this implementation, not a claim that every alternative has already been assigned.

The user likes the current dark, sci-fi visual direction: small lights in a
large night sky, meteor-like traces, continuous camera movement, and detail
emerging as you approach. The important distinction is **one space to explore**,
not cards replacing one another. This is a reference, not a restriction on a
different presentation the user explicitly requests for a variant.

Keep the meaning of the picture fixed: workers, tasks, sessions, claims,
candidates and baselines are different entities. Growth and recombination come
from recorded references and events, not a timed worker-spawning animation.
Do not change the recording, runtime policies, or frozen contracts just to make
an alternative look more active.

| Area | Starting points |
|---|---|
| Overall composition and controls | `src/App.tsx`, `src/replay.css`, `src/night.css` |
| Scene, marks, trails and selection | `src/CausalMap.tsx` |
| Spatial layout and camera | `layout_recording` in `src/replay.ts`, `src/spatial.ts`, `src/useWorldCamera.ts` |
| Inspection | `src/TraceInspector.tsx`, `src/EventInspector.tsx` |
| Shared semantics to preserve | Recording projection and validation in `src/replay.ts`; Python recording/live adapters; generated recording files |

For a comparable visual review, use the same committed recording and capture
the overview at 29 seconds, a multi-parent candidate's Links panel at the end,
and a 390×844 worker inspection. The desktop reference is 1440×900.
`UI_ARTIFACTS_DIR` makes the browser checks save these views.

Baseline screenshots: [overview](visual-baseline/causal-overview.jpg),
[candidate lineage](visual-baseline/causal-lineage.jpg),
[narrow inspection](visual-baseline/causal-narrow.jpg).

## Attach an observer to a real run

For the local runtime, wrap the existing assembly with
`ui.live.RecordingAssembly`, pass it to `build_herd` with the run's existing
configuration, then call:

```python
from pathlib import Path

from ui.live import start_recorded

run = start_recorded(herd, Path("ui/public/recording.json"))
```

This prepares the run, lets its Indexer rebuild, captures the public snapshot,
and then starts workers. The wrapper installs `ObservedExecutor` before worker
dependencies are built. The returned run preserves `events()`, `status()`,
`stop()`, `report()` and `wait()`; stopping or waiting for completion closes
the recorder after the runtime's closeout.

`TraceRecorder` consumes an existing blackboard and Indexer through their public
interfaces. Attach it to a prepared, quiescent run before starting workers:

```python
from pathlib import Path

from ui.replay_recording import TraceRecorder

recorder = TraceRecorder(blackboard, indexer, clock)
recorder.start(
    Path("ui/public/recording.json"),
    worker_ids_including_compose,
    compose_actor,
)
# Start the existing run normally.
# At shutdown:
recorder.stop()
```

It reads the initial public snapshot, subscribes to subsequent merges and
atomically publishes JSON. It never writes to the blackboard, installs hooks,
opens push credentials or reads private evaluation material. A changing initial
snapshot is rejected; attach before execution, not midway through startup.
Pre-attachment artifacts are a snapshot, not invented historical timestamps.

For real task/session observations, wrap the execution port in the assembly,
before worker dependencies are built:

```python
from ui.observed_executor import ObservedExecutor

observed_executor = ObservedExecutor(executor, recorder)
```

The wrapper forwards the Executor contract and records successful starts,
observed exits and interrupt replacements. Restarting a session keeps its task
and worker identities. Checkpoint/state bytes and transcript contents are never
recorded. Without this instrumentation, session state remains **unobserved**;
claim publication does not manufacture a running task or imply adoption.

In the UI, **Provenance → Follow recording.json** follows the published file.
The Vite watcher excludes this file so updates do not reload the page. Following
preserves the selected object; HTTP and schema failures show an error and keep
the last valid recording. **Open recording JSON** loads a saved recording
locally in the browser without uploading it.

The observer does not add context-fork support to the current harness. Design
D5 requires full conversation, sandbox and tool-state inheritance; an ordinary
`result_ready` event instead creates a follow-up task. The UI only draws the
relationships present in its input.

## Current interface coverage

| Surface | Supported boundary |
|---|---|
| Canonical `Event` | Every current `EventType`, all envelope fields and every payload are recorded using the core serializer. Generic events remain inspectable without inventing graph nodes. |
| Local runtime | `RecordingAssembly` / `start_recorded` integrate with the current `LocalHerd`. `events`, `status`, `report`, `stop`, and `wait` retain their runtime results. Captured status/report data is available to the UI. |
| Executor port | Calls delegate to the supplied executor; actual lifecycle observations keep worker and task identity separate. Unsupported checkpoint/restore remains unsupported. |
| Blackboard / Indexer | Public snapshots, ordered merged events and derived claim state are read through existing interfaces. No blackboard writes, private transcript reads, or new judging policy are introduced. |
| Project statement | The graph represents retained evidence, reuse, candidate combination and feedback on a moving baseline. It does not claim to prove H1, implement the scorecard, or implement roadmap features that the runtime itself does not yet provide. |

This is compatibility with the **current implemented local interfaces**, not a
claim that the planned SDK, every future infrastructure adapter, or all
PI/Planner/checkpoint workflows are implemented. Frozen contract files remain
unchanged.

## Verification

```bash
npm test
npm run build
npm run test:recording
npx playwright install chromium
npm run test:browser
```

Alternatively, use an installed Chrome with
`PLAYWRIGHT_CHANNEL=chrome npm run test:browser`.
Browser checks start and close their own local server. Set `UI_ARTIFACTS_DIR`
to retain overview, lineage and narrow-screen screenshots.

The checks cover schema failures, chronological projection, retained failures,
actual multi-parent relationships, worker/session identity, stable geometry,
camera continuity, live-file errors, recording imports and real touch input.
The Python tests also verify that observing a blackboard does not mutate it.
