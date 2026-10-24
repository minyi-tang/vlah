"""The composition root: one arm's herd, built, started, stopped and reported (D42, D58, D62, D67).

build_herd fixes everything a run is made of: one RunContext (run id and root,
ArmConfig, the problem's Params, clock), the components the assembly builds from it,
and one worker slot per worker plus the compose slot. prepare rebuilds the Indexer
from the blackboard once, so a restarted process knows every earlier claim (D13),
then subscribes the merge fan-out; start starts the supervisor thread and returns
the Run.

The merge fan-out, on every merged commit:
- feeds the Indexer and the Router;
- turns result_ready, and only result_ready, into the submitter's follow-up task
  (remeasure_ready belongs to the Integrator), in the compose queue when the
  submitter is the compose slot and in the arm queue otherwise (D74);
- hands a result claim to the Integrator when a claim_status event moves it to
  verified at the top level of the judge ladder, unless the claim is one of the
  Integrator's own remeasures, which the eval service tags "remeasure";
- counts judge runs from each finished candidate's result.json, each candidate
  hash once: one per level the judge ran itself, so a cache hit (a repeat of a
  hash already counted) and a dead-end confirmation do not count.
It runs on the writing thread, so it never calls the executor.

A started run writes its status file after every tick (vlah.runtime.status); the
workers get the run's leases through a ledger that records each session's task.
"""

import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

from vlah.arms.config import PRESETS, ArmName
from vlah.core.blackboard.api import Blackboard, MergedCommit
from vlah.core.metrics.api import StandingMetrics
from vlah.core.problem.api import LoadProblem, Problem
from vlah.core.queue.api import Leases
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, RemeasureReady, ResultReady, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import SourceDecl
from vlah.core.worker.loop import InterruptInbox, WorkerDeps, run_worker
from vlah.core.worker.start_pack import ContextSources
from vlah.ports.harness import Workspace
from vlah.runtime.api import ArmRunRequest, Report, RunStatus
from vlah.runtime.assembly import Assembly, RunComponents, RunContext
from vlah.runtime.compose import COMPOSE_AGENT, ComposeStartPacks
from vlah.runtime.isolation import worker_view
from vlah.runtime.status import (
    STATUS_FILE_NAME,
    LedgerLeases,
    RunActivity,
    SessionLedger,
    build_status,
    write_status,
)
from vlah.runtime.supervisor import (
    STOP_REQUEST_FILE_NAME,
    Supervisor,
    WorkerRunner,
    WorkerSlot,
)

logger = logging.getLogger(__name__)

DEFAULT_SUPERVISOR_POLL_SECONDS = 5.0
SECONDS_PER_HOUR = 3600.0
VERIFIED_PREFIX = "verified:"
EVAL_SERVICE = "eval_service"
# The eval service tags the result claim of an Integrator remeasure (D74).
REMEASURE_TAG = "remeasure"


def worker_id(number: int) -> ActorId:
    return ActorId(f"worker-{number:02d}")


def top_level_source(sources: tuple[SourceDecl, ...]) -> str | None:
    """The judge ladder's last level, derived as the eval service derives its ladder:
    pull sources sorted by cost then latency, the last of equals (D9)."""
    pull = [source for source in sources if source.mode == "pull"]
    if not pull:
        return None
    else:
        ladder = sorted(
            pull, key=lambda source: (source.cost_machine_hours, source.latency_seconds)
        )
        return ladder[-1].id


def judge_runs_in(blackboard: Blackboard, candidate_hash: str) -> list[str]:
    """The levels of candidates/<hash>/result.json the judge ran itself, by source
    id: a level answered from the judge's cache is not a run (DECISIONS: judge cache
    hits)."""
    path = f"candidates/{candidate_hash}/result.json"
    if path not in blackboard.list_paths(path, None):
        return []
    else:
        result: object = json.loads(blackboard.read(path, None))
        levels = result.get("levels", []) if isinstance(result, dict) else []
        return [
            str(level.get("level"))
            for level in levels
            if isinstance(level, dict) and not level.get("is_cached", False)
        ]


def is_remeasure_claim(blackboard: Blackboard, claim_id: str) -> bool:
    path = f"claims/{EVAL_SERVICE}/{claim_id}.md"
    if path not in blackboard.list_paths(path, None):
        return False
    else:
        claim = parse_claim(blackboard.read(path, None).decode("utf-8"))
        return REMEASURE_TAG in claim.tags


@dataclass(kw_only=True)
class RunFanout:
    components: RunComponents
    top_level: str | None
    # Summed as candidates finish, so status() never re-reads the blackboard.
    judge_runs_by_source: dict[str, int] = field(default_factory=dict[str, int])
    counted_hashes: set[str] = field(default_factory=set[str])

    @property
    def judge_runs(self) -> int:
        return sum(self.judge_runs_by_source.values())

    def __call__(self, merged_commit: MergedCommit) -> None:
        self.components.indexer.apply(merged_commit)
        self.components.router.on_commit(merged_commit)
        for event in merged_commit.events:
            finished = event.data
            if (
                isinstance(finished, (ResultReady, RemeasureReady))
                and finished.candidate not in self.counted_hashes
            ):
                self.counted_hashes.add(finished.candidate)
                for source in judge_runs_in(
                    self.components.blackboard, finished.candidate
                ):
                    self.judge_runs_by_source[source] = (
                        self.judge_runs_by_source.get(source, 0) + 1
                    )
            else:
                pass
            if event.type == "result_ready":
                is_compose_result = (
                    isinstance(event.data, ResultReady)
                    and event.data.submitter == COMPOSE_AGENT
                )
                queue = (
                    self.components.compose_queue
                    if is_compose_result
                    else self.components.task_queue
                )
                queue.on_result_ready(event)
            elif (
                event.type == "claim_status"
                and event.target is not None
                and event.to == f"{VERIFIED_PREFIX}{self.top_level}"
                and not is_remeasure_claim(self.components.blackboard, event.target)
            ):
                self.components.integrator.on_verified(event.target)
            else:
                pass


def events_on(blackboard: Blackboard) -> Iterator[Event]:
    """Every merged event, in sequence order, with the number it was given."""
    for sequence in range(1, blackboard.head_sequence() + 1):
        path = blackboard.read(f"events/_seq/{sequence}", None).decode()
        event = event_from_json(blackboard.read(path, None).decode())
        yield event.model_copy(update={"seq": sequence})


def baselines_on(blackboard: Blackboard) -> tuple[BaselineRecord, ...]:
    records = [
        BaselineRecord.model_validate_json(blackboard.read(path, None))
        for path in blackboard.list_paths("baseline/", None)
        if path.endswith(".json")
    ]
    return tuple(sorted(records, key=lambda record: record.k))


class HerdRun:
    """A started run (runtime.api.Run)."""

    def __init__(
        self,
        *,
        context: RunContext,
        components: RunComponents,
        supervisor: Supervisor,
        fanout: RunFanout,
        ledger: SessionLedger,
        activity: RunActivity,
        worker_count: int,
        poll_seconds: float,
    ) -> None:
        self.context = context
        self.components = components
        self.supervisor = supervisor
        self.fanout = fanout
        self.ledger = ledger
        self.activity = activity
        self.worker_count = worker_count
        self.poll_seconds = poll_seconds
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self.supervise, name=f"supervisor-{context.run_id}", daemon=True
        )

    @property
    def status_path(self) -> Path:
        return self.context.run_root / STATUS_FILE_NAME

    def write_status(self) -> None:
        """A status that cannot be written is logged; it never stops the run."""
        try:
            write_status(
                self.status_path,
                build_status(
                    context=self.context,
                    components=self.components,
                    supervisor=self.supervisor,
                    ledger=self.ledger,
                    activity=self.activity,
                    judge_runs_by_source=dict(self.fanout.judge_runs_by_source),
                    poll_seconds=self.poll_seconds,
                ),
            )
        except Exception:
            logger.exception(f"could not write {self.status_path}")

    def tick(self, now_seconds: float) -> tuple[Event, ...]:
        """One supervisor tick, then the status file."""
        events = self.supervisor.tick(now_seconds)
        self.write_status()
        return events

    def close(self) -> None:
        """Finish the run, writing the status as closing starts and once it ends."""
        self.write_status()
        try:
            self.supervisor.finish()
        finally:
            self.write_status()

    def supervise(self) -> None:
        try:
            while not self.supervisor.is_stopping:
                try:
                    self.tick(self.context.clock.now_seconds())
                except Exception:
                    logger.exception("the supervisor's tick failed; it keeps going")
                self.supervisor.stop_signal.wait(self.poll_seconds)
            self.close()
        finally:
            self.finished.set()

    def events(self) -> Iterator[Event]:
        return events_on(self.components.blackboard)

    def status(self) -> RunStatus:
        blackboard = self.components.blackboard
        return RunStatus(
            run_id=self.context.run_id,
            arm=self.context.arm.arm,
            is_running=not self.finished.is_set(),
            head_sequence=blackboard.head_sequence(),
            current_baseline=self.components.integrator.current().k,
            judge_runs_used=self.fanout.judge_runs,
            worker_count=self.worker_count,
        )

    def stop(self) -> None:
        """Stop cleanly: kill the sessions, then close out the Integrator once."""
        self.supervisor.request_stop("stop() was called", "human")
        if self.thread.ident is None:
            # Never started: there is no supervisor thread to finish the run.
            try:
                self.close()
            finally:
                self.finished.set()
        else:
            self.finished.wait()

    def wait(self, timeout_seconds: float | None) -> bool:
        return self.finished.wait(timeout_seconds)

    def report(self) -> Report:
        """The deliverable is the closeout's baseline once the run has stopped."""
        if self.supervisor.stop_state.error is not None:
            raise RuntimeError(self.supervisor.stop_state.error)
        else:
            pass
        deliverable = self.supervisor.stop_state.deliverable
        metrics: StandingMetrics = self.components.metrics.standing()
        return Report(
            run_id=self.context.run_id,
            arm=self.context.arm.arm,
            deliverable=deliverable or self.components.integrator.current(),
            baselines=baselines_on(self.components.blackboard),
            metrics=metrics,
        )


@dataclass(frozen=True, kw_only=True)
class LocalHerd:
    """One arm's herd, ready to start (runtime.api.Herd)."""

    context: RunContext
    assembly: Assembly
    worker_count: int
    runner: WorkerRunner
    poll_seconds: float

    def worker_deps(
        self,
        agent: ActorId,
        components: RunComponents,
        leases: Leases,
        inbox: InterruptInbox,
        stop_signal: threading.Event,
        is_compose: bool,
    ) -> WorkerDeps:
        context = self.context
        view = (
            components.blackboard
            if is_compose
            else worker_view(components.blackboard, context.arm, context.run_id, agent)
        )
        return WorkerDeps(
            task_queue=(
                components.compose_queue if is_compose else components.task_queue
            ),
            leases=leases,
            executor=components.executor,
            harness=components.harness,
            tool_server=(
                components.compose_tool_server if is_compose else components.tool_server
            ),
            context=ContextSources(
                blackboard=components.blackboard,
                indexer=components.indexer,
                arm=context.arm,
                params=context.params,
            ),
            inbox=inbox,
            workspace=Workspace(
                target_repository=components.target_repository,
                target_ref=components.target_ref,
                blackboard_clone_url=view.clone_url(),
            ),
            clock=context.clock,
            stop=stop_signal,
            start_packs=(
                ComposeStartPacks(blackboard=components.blackboard)
                if is_compose
                else None
            ),
        )

    def prepare(self) -> HerdRun:
        """Build every component and slot without starting anything."""
        context = self.context
        components = self.assembly.build(context)
        fanout = RunFanout(
            components=components,
            top_level=top_level_source(context.problem.sources),
        )
        activity = RunActivity(clock=context.clock)
        # A fresh Indexer knows nothing merged before this process started.
        components.indexer.rebuild()
        components.blackboard.subscribe(fanout)
        components.blackboard.subscribe(activity)
        stale_request = context.run_root / STOP_REQUEST_FILE_NAME
        if stale_request.is_file():
            logger.warning(f"removing {stale_request}, left by an earlier process")
            stale_request.unlink()
        else:
            pass
        ledger = SessionLedger()
        leases = LedgerLeases(leases=components.leases, ledger=ledger)
        inbox = InterruptInbox()
        stop_signal = threading.Event()
        slots = tuple(
            WorkerSlot(
                agent=agent,
                deps=self.worker_deps(
                    agent, components, leases, inbox, stop_signal, is_compose
                ),
                is_compose=is_compose,
            )
            for agent, is_compose in [
                *(
                    (worker_id(number), False)
                    for number in range(1, self.worker_count + 1)
                ),
                (COMPOSE_AGENT, True),
            ]
        )
        supervisor = Supervisor(
            context=context,
            components=components,
            slots=slots,
            inbox=inbox,
            runner=self.runner,
            stop_signal=stop_signal,
            started_seconds=context.clock.now_seconds(),
        )
        run = HerdRun(
            context=context,
            components=components,
            supervisor=supervisor,
            fanout=fanout,
            ledger=ledger,
            activity=activity,
            worker_count=self.worker_count,
            poll_seconds=self.poll_seconds,
        )
        return run

    def start(self) -> HerdRun:
        run = self.prepare()
        run.thread.start()
        return run


def build_herd(
    problem: Problem,
    arm: ArmName,
    assembly: Assembly,
    run_id: str,
    *,
    root: Path,
    worker_count: int,
    duration_seconds: float,
    clock: Clock,
    random: Random,
    runner: WorkerRunner = run_worker,
    poll_seconds: float = DEFAULT_SUPERVISOR_POLL_SECONDS,
) -> LocalHerd:
    """Every arm gets the problem's Params unchanged, so the Integrator and every
    other component are configured identically across arms (D58, L0-19)."""
    if arm == "B0" and worker_count != 1:
        raise ValueError(f"B0 is one agent (D58), not {worker_count} workers")
    else:
        pass
    return LocalHerd(
        context=RunContext(
            run_id=run_id,
            run_root=root / run_id,
            arm=PRESETS[arm],
            params=problem.params,
            problem=problem,
            clock=clock,
            random=random,
            duration_seconds=duration_seconds,
        ),
        assembly=assembly,
        worker_count=worker_count,
        runner=runner,
        poll_seconds=poll_seconds,
    )


@dataclass(frozen=True, kw_only=True)
class AssembledRunArm:
    """runtime.api.RunArm over a problem loader and an assembly."""

    load_problem: LoadProblem
    assembly: Assembly
    root: Path
    clock: Clock
    random: Random

    def __call__(self, request: ArmRunRequest) -> Report:
        herd = build_herd(
            self.load_problem(request.problem_root),
            request.arm,
            self.assembly,
            request.run_id,
            root=self.root,
            worker_count=request.worker_count,
            duration_seconds=request.duration_hours * SECONDS_PER_HOUR,
            clock=self.clock,
            random=self.random,
        )
        run = herd.start()
        run.wait(None)
        return run.report()
