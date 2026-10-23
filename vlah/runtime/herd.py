"""The composition root: one arm's herd, built, started, stopped and reported (D42, D58, D62, D67).

build_herd fixes everything a run is made of: one RunContext (run id and root,
ArmConfig, the problem's Params, clock), the components the assembly builds from it,
and one worker slot per worker plus the compose slot. start subscribes the merge
fan-out, starts the supervisor thread, and returns the Run.

The merge fan-out, on every merged commit:
- feeds the Indexer and the Router;
- turns result_ready, and only result_ready, into the submitter's follow-up task
  (remeasure_ready belongs to the Integrator);
- hands a result claim to the Integrator when a claim_status event moves it to
  verified at the top level of the judge ladder.
It runs on the writing thread, so it never calls the executor.
"""

import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from random import Random

from vlah.arms.config import PRESETS, ArmName
from vlah.core.blackboard.api import Blackboard, MergedCommit
from vlah.core.metrics.api import StandingMetrics
from vlah.core.problem.api import LoadProblem, Problem
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import SourceDecl
from vlah.core.worker.loop import InterruptInbox, WorkerDeps, run_worker
from vlah.core.worker.start_pack import ContextSources
from vlah.ports.errors import NotSupportedError
from vlah.ports.harness import Workspace
from vlah.runtime.api import ArmRunRequest, Report, RunStatus
from vlah.runtime.assembly import Assembly, RunComponents, RunContext
from vlah.runtime.compose import COMPOSE_AGENT, ComposeStartPacks
from vlah.runtime.isolation import worker_view
from vlah.runtime.supervisor import Supervisor, WorkerRunner, WorkerSlot

logger = logging.getLogger(__name__)

DEFAULT_SUPERVISOR_POLL_SECONDS = 5.0
SECONDS_PER_HOUR = 3600.0
VERIFIED_PREFIX = "verified:"
EVAL_SERVICE = "eval_service"


def worker_id(number: int) -> ActorId:
    return ActorId(f"worker-{number:02d}")


def top_level_source(sources: tuple[SourceDecl, ...]) -> str | None:
    """The judge ladder's last level: the most expensive pull source (D9)."""
    pull = [source for source in sources if source.mode == "pull"]
    if not pull:
        return None
    else:
        return max(
            pull, key=lambda source: (source.cost_machine_hours, source.latency_seconds)
        ).id


@dataclass(kw_only=True)
class RunFanout:
    components: RunComponents
    top_level: str | None
    # One per measured judge level, so status() never re-reads the blackboard.
    judge_runs: int = 0

    def __call__(self, merged_commit: MergedCommit) -> None:
        self.components.indexer.apply(merged_commit)
        self.components.router.on_commit(merged_commit)
        for event in merged_commit.events:
            if event.type == "claim_status" and event.actor == EVAL_SERVICE:
                self.judge_runs += 1
            else:
                pass
            if event.type == "result_ready":
                self.components.task_queue.on_result_ready(event)
            elif (
                event.type == "claim_status"
                and event.target is not None
                and event.to == f"{VERIFIED_PREFIX}{self.top_level}"
            ):
                self.components.integrator.on_verified(event.target)
            else:
                pass


def events_on(blackboard: Blackboard) -> Iterator[Event]:
    """Every merged event, in sequence order."""
    for sequence in range(1, blackboard.head_sequence() + 1):
        path = blackboard.read(f"events/_seq/{sequence}", None).decode()
        yield event_from_json(blackboard.read(path, None).decode())


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
        worker_count: int,
        poll_seconds: float,
    ) -> None:
        self.context = context
        self.components = components
        self.supervisor = supervisor
        self.fanout = fanout
        self.worker_count = worker_count
        self.poll_seconds = poll_seconds
        self.finished = threading.Event()
        self.thread = threading.Thread(
            target=self.supervise, name=f"supervisor-{context.run_id}", daemon=True
        )

    def supervise(self) -> None:
        try:
            while not self.supervisor.is_stopping:
                try:
                    self.supervisor.tick(self.context.clock.now_seconds())
                except Exception:
                    logger.exception("the supervisor's tick failed; it keeps going")
                self.supervisor.stop_signal.wait(self.poll_seconds)
            self.supervisor.finish()
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
        self.supervisor.request_stop("stop() was called")
        if self.thread.ident is None:
            # Never started: there is no supervisor thread to finish the run.
            try:
                self.supervisor.finish()
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
            leases=components.leases,
            executor=components.executor,
            harness=components.harness,
            tool_server=components.tool_server,
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
        components.blackboard.subscribe(fanout)
        inbox = InterruptInbox()
        stop_signal = threading.Event()
        slots = tuple(
            WorkerSlot(
                agent=agent,
                deps=self.worker_deps(
                    agent, components, inbox, stop_signal, is_compose
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


def run_demo(seed: int) -> Report:
    """runtime.api.RunDemo. It needs in-package Router, Indexer, Integrator, budget,
    stop rules and metrics, which do not exist yet (#12, #15, #19, #20)."""
    raise NotSupportedError(
        "vlah demo waits for the Router (#15), Indexer (#12), Integrator (#19) and "
        "budget, stop rules and metrics (#20)"
    )
