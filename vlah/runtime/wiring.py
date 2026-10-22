"""A herd composed for tests and the demo, stepped on a fake clock.

Seeded by the Foundation (#9); the runtime (W1-11, #21) owns and extends it. It
wires the blackboard's merge hook to the indexer, the router and the follow-up
tasks for results (D28), starts idle workers on tasks, and moves time forward.
"""

from dataclasses import dataclass, field

from vlah.adapters.memory.executor import MemoryExecutor
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard, MergedCommit
from vlah.core.eval.api import EvalService
from vlah.core.indexer.api import Indexer
from vlah.core.queue.api import Leases, TaskQueue
from vlah.core.router.api import Router
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.ports.executor import Handle, StartPack
from vlah.ports.harness import Harness, HookConfig, ToolEndpoint, Workspace


@dataclass(frozen=True, kw_only=True)
class HerdComponents:
    """One implementation per component contract: real ones, or the test fakes."""

    blackboard: Blackboard
    indexer: Indexer
    task_queue: TaskQueue
    leases: Leases
    eval_service: EvalService
    router: Router


@dataclass(frozen=True, kw_only=True)
class RunningTask:
    handle: Handle
    task: Task


@dataclass(frozen=True, kw_only=True)
class CommitFanout:
    """The blackboard's merge hook: feed the indexer and router, and queue result follow-ups."""

    components: HerdComponents

    def __call__(self, merged_commit: MergedCommit) -> None:
        self.components.indexer.apply(merged_commit)
        self.components.router.on_commit(merged_commit)
        for event in merged_commit.events:
            if event.type == "result_ready":
                self.components.task_queue.on_result_ready(event)
            else:
                pass


@dataclass(kw_only=True)
class SteppedHerd:
    arm: ArmConfig
    params: Params
    clock: FakeClock
    executor: MemoryExecutor
    harness: Harness
    components: HerdComponents
    worker_ids: tuple[ActorId, ...]
    workspace: Workspace
    running: dict[ActorId, RunningTask] = field(
        default_factory=dict[ActorId, RunningTask]
    )

    def start_idle_workers(self) -> None:
        for agent in self.worker_ids:
            if agent in self.running:
                continue
            else:
                task = self.components.task_queue.claim_task(agent)
                if task is None:
                    continue
                else:
                    self.start_worker(agent, task)

    def start_worker(self, agent: ActorId, task: Task) -> None:
        start_pack = StartPack(files={"CONTEXT.md": task.goal})
        spec = self.harness.build(
            agent,
            task,
            start_pack,
            ToolEndpoint(url="memory://tools", bearer_token=f"token-{agent}"),
            HookConfig(
                interrupt_check_url="memory://tools/hook/check",
                interrupt_ack_url="memory://tools/hook/ack",
            ),
            self.workspace,
        )
        handle = self.executor.start(spec, task, start_pack, self.components.leases)
        self.components.leases.acquire(agent, task, handle.handle_id)
        self.running[agent] = RunningTask(handle=handle, task=task)

    def finish_exited_workers(self) -> None:
        for agent, running_task in list(self.running.items()):
            if self.executor.is_alive(running_task.handle):
                continue
            else:
                self.components.leases.release(agent)
                self.components.task_queue.complete(running_task.task.id, agent)
                self.running.pop(agent)

    def step(self, seconds: float) -> None:
        """Start idle workers, move time, let the judges report, and retire finished workers."""
        self.start_idle_workers()
        self.executor.advance(seconds)
        self.components.eval_service.tick()
        self.finish_exited_workers()


def build_stepped_herd(
    *,
    arm: ArmConfig,
    params: Params,
    clock: FakeClock,
    executor: MemoryExecutor,
    harness: Harness,
    components: HerdComponents,
    worker_ids: tuple[ActorId, ...],
    workspace: Workspace,
) -> SteppedHerd:
    """Subscribe the merge hook and queue the first tasks; the caller then calls step."""
    components.blackboard.subscribe(CommitFanout(components=components))
    components.task_queue.maintain(params.slots_per_approach)
    return SteppedHerd(
        arm=arm,
        params=params,
        clock=clock,
        executor=executor,
        harness=harness,
        components=components,
        worker_ids=worker_ids,
        workspace=workspace,
    )
