"""The supervisor: keeps the plain-code services moving and the workers running (D32, D42, D60).

tick does whatever is due at the injected clock's time, so tests call it directly:
1. TaskQueue.maintain with the fixed slots in params.slots_per_approach (D42 rule 1);
2. restart any worker thread that died, unless the run is stopping;
3. Leases.reclaim_stale, asking the executor whether each handle still runs;
4. Router.overdue -> the InterruptInbox; each worker loop then calls
   Executor.interrupt itself, because the loop owns its session's handle;
5. EvalService.tick and IntegratorService.tick;
6. StopRules.evaluate, and the run's time limit.

Stopping is idempotent: the first request sets the workers' stop signal and kills
their sessions; finish then joins the workers and runs the Integrator's closeout
exactly once.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Protocol

from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.worker.loop import InterruptInbox, WorkerDeps
from vlah.ports.executor import Handle
from vlah.runtime.assembly import RunComponents, RunContext

logger = logging.getLogger(__name__)

JOIN_TIMEOUT_SECONDS = 60.0


class WorkerRunner(Protocol):
    def __call__(self, agent: ActorId, deps: WorkerDeps) -> None:
        """Run tasks for agent until deps.stop is set (run_worker in production)."""
        ...


@dataclass(kw_only=True)
class WorkerSlot:
    agent: ActorId
    deps: WorkerDeps
    is_compose: bool = False
    thread: threading.Thread | None = None
    crash_count: int = 0


@dataclass(kw_only=True)
class StopState:
    reason: str | None = None
    deliverable: BaselineRecord | None = None
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Supervisor:
    def __init__(
        self,
        *,
        context: RunContext,
        components: RunComponents,
        slots: tuple[WorkerSlot, ...],
        inbox: InterruptInbox,
        runner: WorkerRunner,
        stop_signal: threading.Event,
        started_seconds: float,
    ) -> None:
        self.context = context
        self.components = components
        self.slots = slots
        self.inbox = inbox
        self.runner = runner
        self.stop_signal = stop_signal
        self.deadline_seconds = started_seconds + context.duration_seconds
        self.stop_state = StopState()
        self.events: list[Event] = []

    @property
    def is_stopping(self) -> bool:
        return self.stop_signal.is_set()

    def run_slot(self, slot: WorkerSlot) -> None:
        try:
            self.runner(slot.agent, slot.deps)
        except Exception:
            slot.crash_count += 1
            logger.exception(f"worker {slot.agent} crashed; the supervisor restarts it")

    def keep_workers_running(self) -> None:
        for slot in self.slots:
            is_running = slot.thread is not None and slot.thread.is_alive()
            if is_running or self.is_stopping:
                continue
            else:
                slot.thread = threading.Thread(
                    target=self.run_slot,
                    args=(slot,),
                    name=f"worker-loop-{slot.agent}",
                    daemon=True,
                )
                slot.thread.start()

    def is_handle_alive(self, handle_id: str) -> bool:
        """Liveness for the queue's reclaimer, from the lease that names the handle."""
        for lease in self.components.leases.active():
            if lease.handle_id == handle_id:
                return self.components.executor.is_alive(
                    Handle(
                        handle_id=handle_id, agent=lease.agent, task_id=lease.task_id
                    )
                )
            else:
                continue
        return False

    def check_stop(self, now_seconds: float) -> list[Event]:
        """The stop rules and the time limit come first, so a failing step cannot
        keep a run going past its limit."""
        try:
            stop_event = self.components.stop_rules.evaluate(now_seconds)
        except Exception:
            logger.exception("the stop rules failed; the time limit still applies")
            stop_event = None
        if stop_event is not None and stop_event.type == "stopped":
            self.request_stop(f"stop rule: {stop_event.data}")
        elif now_seconds >= self.deadline_seconds:
            self.request_stop("the run's time limit")
        else:
            pass
        return [stop_event] if stop_event is not None else []

    def maintain_tasks(self, now_seconds: float) -> list[Event]:
        self.components.task_queue.maintain(self.context.params.slots_per_approach)
        return []

    def supervise_workers(self, now_seconds: float) -> list[Event]:
        self.keep_workers_running()
        return []

    def reclaim_leases(self, now_seconds: float) -> list[Event]:
        self.components.leases.reclaim_stale(now_seconds, self.is_handle_alive)
        return []

    def deliver_overdue(self, now_seconds: float) -> list[Event]:
        for interrupt in self.components.router.overdue(now_seconds):
            self.inbox.post(interrupt)
        return []

    def tick_eval(self, now_seconds: float) -> list[Event]:
        return list(self.components.eval_service.tick())

    def tick_integrator(self, now_seconds: float) -> list[Event]:
        return list(self.components.integrator.tick())

    def tick(self, now_seconds: float) -> tuple[Event, ...]:
        """Each step runs on its own: one failing step never skips the others."""
        events = self.check_stop(now_seconds)
        for step in (
            self.maintain_tasks,
            self.supervise_workers,
            self.reclaim_leases,
            self.deliver_overdue,
            self.tick_eval,
            self.tick_integrator,
        ):
            try:
                events.extend(step(now_seconds))
            except Exception:
                logger.exception(f"supervisor step {step.__name__} failed")
        self.events.extend(events)
        return tuple(events)

    def request_stop(self, reason: str) -> None:
        """Ask every worker to stop now; only the first request counts."""
        with self.stop_state.lock:
            if self.stop_state.reason is not None:
                return
            else:
                self.stop_state.reason = reason
                logger.info(f"run {self.context.run_id} stopping: {reason}")
                self.stop_signal.set()
        self.kill_leased_sessions()

    def kill_leased_sessions(self) -> None:
        for lease in self.components.leases.active():
            handle = Handle(
                handle_id=lease.handle_id, agent=lease.agent, task_id=lease.task_id
            )
            try:
                self.components.executor.kill(handle)
            except Exception:
                logger.exception(f"could not kill {handle.handle_id}; stopping anyway")

    def finish(self) -> BaselineRecord:
        """Join the workers and run the Integrator's closeout, exactly once (D79).

        A session that started after the first kill is killed here, once the
        workers can no longer start any.
        """
        self.request_stop(self.stop_state.reason or "finish")
        for slot in self.slots:
            if slot.thread is not None:
                slot.thread.join(JOIN_TIMEOUT_SECONDS)
            else:
                pass
        self.kill_leased_sessions()
        with self.stop_state.lock:
            if self.stop_state.deliverable is None:
                try:
                    self.stop_state.deliverable = self.components.integrator.closeout()
                except Exception as error:
                    self.stop_state.error = f"closeout failed: {error!r}"
                    raise
            else:
                pass
            return self.stop_state.deliverable
