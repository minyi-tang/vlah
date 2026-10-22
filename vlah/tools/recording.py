"""Recorder events for the testbed scorecard (TB section 11.4), sent with no lock held.

A recorder failure is logged and swallowed: by the time an event is sent, the write
it describes has merged, and the tool call must not look failed.
"""

import logging
from dataclasses import dataclass

from pydantic import JsonValue

from vlah.arms.config import ArmConfig
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.tools.api import Recorder, RecorderEvent, RecorderEventType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PendingEvent:
    type: RecorderEventType
    data: dict[str, JsonValue]


@dataclass(frozen=True, kw_only=True)
class RecorderClient:
    recorder: Recorder | None
    clock: Clock
    run_id: str
    arm: ArmConfig

    def post(self, agent: ActorId, events: list[PendingEvent]) -> None:
        if self.recorder is None or not events:
            return
        else:
            try:
                self.recorder.post(
                    tuple(
                        RecorderEvent(
                            ts=self.clock.now_seconds(),
                            run_id=self.run_id,
                            arm=self.arm.arm,
                            actor=agent,
                            type=event.type,
                            data=event.data,
                        )
                        for event in events
                    )
                )
            except Exception:
                logger.exception(
                    f"the recorder dropped {len(events)} events from {agent}"
                )
