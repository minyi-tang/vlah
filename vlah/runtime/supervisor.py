"""The supervisor: keeps the plain-code services moving and the workers running (D32, D42, D60).

tick does whatever is due at the injected clock's time, so tests call it directly:
1. TaskQueue.maintain with the fixed slots in params.slots_per_approach (D42 rule 1);
2. restart any worker thread that died, unless the run is stopping;
3. Leases.reclaim_stale, asking the executor whether each handle still runs;
4. Router.overdue -> the InterruptInbox; each worker loop then calls
   Executor.interrupt itself, because the loop owns its session's handle;
5. EvalService.tick, the Critic's tick (which passes verified claims on to the
   Integrator) and IntegratorService.tick;
6. StopRules.evaluate, the run's time limit, and a stop request from vlah stop.

Stopping is idempotent: the first request sets the workers' stop signal and kills
their sessions; finish then joins the workers, stops the Critic, runs the
Integrator's closeout exactly once and has the Critic score the sealed verdict on
the deliverable (D66, D79). A stop rule writes its own stopped event (D52); the
time limit writes stopped(reason="time"), and a stop() call or a stop request
stopped(reason="human"), here, as the actor ci.

vlah stop runs in another process, so it asks by writing STOP_REQUEST_FILE_NAME in
the run root; the tick that finds it removes it and stops the run.
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import Literal, Protocol

from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import Event, event_to_json
from vlah.core.types.ids import ActorId, format_event_local_id
from vlah.core.worker.loop import InterruptInbox, WorkerDeps
from vlah.ports.executor import Handle
from vlah.roles.critic.holdout import HoldoutCritic
from vlah.runtime.assembly import RunComponents, RunContext

logger = logging.getLogger(__name__)

JOIN_TIMEOUT_SECONDS = 60.0
STOP_ACTOR = ActorId("ci")
# The stop rules number ci events from the key-value store; a number they took
# is skipped.
STOP_EVENT_ATTEMPTS = 5
STOP_REQUEST_FILE_NAME = "stop-request.json"

StoppedReason = Literal["human", "time"]


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
        self.started_seconds = started_seconds
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
        stop_request = self.context.run_root / STOP_REQUEST_FILE_NAME
        if stop_event is not None and stop_event.type == "stopped":
            self.request_stop(f"stop rule: {stop_event.data}", None)
        elif now_seconds >= self.deadline_seconds:
            self.request_stop("the run's time limit", "time")
        elif stop_request.is_file():
            stop_request.unlink(missing_ok=True)
            self.request_stop("vlah stop", "human")
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

    def tick_critic(self, now_seconds: float) -> list[Event]:
        critic = self.components.critic
        if critic is not None:
            return list(critic.tick())
        else:
            return []

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
            self.tick_critic,
            self.tick_integrator,
        ):
            try:
                events.extend(step(now_seconds))
            except Exception:
                logger.exception(f"supervisor step {step.__name__} failed")
        self.events.extend(events)
        return tuple(events)

    def request_stop(self, reason: str, stopped_reason: StoppedReason | None) -> None:
        """Ask every worker to stop now; only the first request counts.

        stopped_reason is written as a stopped event; None when a stop rule
        already wrote its own.
        """
        with self.stop_state.lock:
            if self.stop_state.reason is not None:
                return
            else:
                self.stop_state.reason = reason
                logger.info(f"run {self.context.run_id} stopping: {reason}")
                self.stop_signal.set()
        if stopped_reason is not None:
            self.write_stopped(stopped_reason)
        else:
            pass
        self.kill_leased_sessions()

    def write_stopped(self, reason: StoppedReason) -> None:
        """stopped(reason) as the actor ci, numbered after the ci events on the blackboard."""
        blackboard = self.components.blackboard
        first_number = len(blackboard.list_paths(f"events/{STOP_ACTOR}/", None)) + 1
        for number in range(first_number, first_number + STOP_EVENT_ATTEMPTS):
            local_id = format_event_local_id(STOP_ACTOR, number)
            event = Event.model_validate(
                {
                    "local_id": local_id,
                    "ts": self.context.clock.now_seconds(),
                    "actor": STOP_ACTOR,
                    "role": "ci",
                    "type": "stopped",
                    "reason": self.stop_state.reason,
                    "data": {"reason": reason},
                }
            )
            receipt = blackboard.write(
                STOP_ACTOR,
                {f"events/{local_id}.json": event_to_json(event).encode("utf-8")},
                f"stopped: {reason}",
            )
            if receipt.status != "rejected":
                self.events.append(event)
                return
            elif all(
                violation.rule == "file_exists" for violation in receipt.violations
            ):
                continue
            else:
                logger.error(
                    f"the blackboard rejected the stop event: {receipt.violations}"
                )
                return
        logger.error(f"no free ci event number after {STOP_EVENT_ATTEMPTS} attempts")

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
        self.request_stop(self.stop_state.reason or "finish", "human")
        for slot in self.slots:
            if slot.thread is not None:
                slot.thread.join(JOIN_TIMEOUT_SECONDS)
            else:
                pass
        self.kill_leased_sessions()
        with self.stop_state.lock:
            if self.stop_state.deliverable is None:
                critic = self.components.critic
                if critic is not None:
                    critic.stop()
                else:
                    pass
                try:
                    self.stop_state.deliverable = self.components.integrator.closeout()
                except Exception as error:
                    self.stop_state.error = f"closeout failed: {error!r}"
                    raise
                if critic is not None:
                    self.score_verdict(critic, self.stop_state.deliverable)
                else:
                    pass
            else:
                pass
            return self.stop_state.deliverable

    def score_verdict(self, critic: HoldoutCritic, deliverable: BaselineRecord) -> None:
        """A failed verdict leaves the run without a holdout score, never without
        its deliverable."""
        try:
            critic.score_verdict(deliverable)
        except Exception:
            logger.exception("the sealed verdict failed; the deliverable stands")
