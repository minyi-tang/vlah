"""Attach recording without changing the local runtime or any frozen contract."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from ui.observed_executor import ObservedExecutor
from ui.replay_recording import TraceRecorder
from vlah.core.types.events import Event
from vlah.runtime.api import Report, RunStatus
from vlah.runtime.assembly import Assembly, RunComponents, RunContext
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import HerdRun, LocalHerd

logger = logging.getLogger(__name__)


@dataclass
class RecordingAssembly:
    assembly: Assembly
    recorders: dict[str, TraceRecorder] = field(default_factory=dict)

    def build(self, context: RunContext) -> RunComponents:
        components = self.assembly.build(context)
        recorder = TraceRecorder(
            components.blackboard,
            components.indexer,
            context.clock,
            name=context.run_id,
            provenance=f"Observed VLAH run {context.run_id}: public snapshot, merged artifacts, and executor lifecycle observations. Positions are illustrative.",
        )
        self.recorders[context.run_id] = recorder
        return replace(
            components, executor=ObservedExecutor(components.executor, recorder)
        )


@dataclass
class RecordedRun:
    run: HerdRun
    recorder: TraceRecorder

    def events(self) -> Iterator[Event]:
        return self.run.events()

    def status(self) -> RunStatus:
        status = self.run.status()
        try:
            self.recorder.record_status(status)
        except (OSError, TypeError, ValueError):
            logger.exception(
                "could not publish RunStatus; the runtime result is unchanged"
            )
        return status

    def record_finished(self) -> None:
        self.status()
        try:
            self.report()
        except RuntimeError as error:
            with self.recorder.lock:
                self.recorder.report_error = str(error)
        else:
            pass
        try:
            self.recorder.stop()
        except (OSError, TypeError, ValueError):
            logger.exception(
                "could not publish the final recording; the runtime has stopped"
            )

    def stop(self) -> None:
        self.run.stop()
        self.record_finished()

    def report(self) -> Report:
        report = self.run.report()
        try:
            self.recorder.record_report(report)
        except (OSError, TypeError, ValueError):
            logger.exception(
                "could not publish Report; the runtime result is unchanged"
            )
        return report

    def wait(self, timeout_seconds: float | None) -> bool:
        finished = self.run.wait(timeout_seconds)
        if finished:
            self.record_finished()
        else:
            pass
        return finished


def start_recorded(herd: LocalHerd, output_path: Path) -> RecordedRun:
    if not isinstance(herd.assembly, RecordingAssembly):
        raise ValueError("Build this herd with RecordingAssembly before starting it.")
    else:
        run = herd.prepare()
        recorder = herd.assembly.recorders[run.context.run_id]
        recorder.start(
            output_path,
            tuple(slot.agent for slot in run.supervisor.slots),
            COMPOSE_AGENT,
        )
        run.thread.start()
        observed = RecordedRun(run, recorder)
        observed.status()
        return observed
