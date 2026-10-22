"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The MCP tool server: the only way workers reach the herd, and the only holder of
push credentials (v2.2 section 7, D60 layer 1, D68). Implemented by W1-06 (#16).

The interrupt hook endpoints are HTTP: POST /hook/check with the agent's token
returns the pending hard interrupt or null within 50 ms; POST /hook/ack
acknowledges one.
"""

from typing import Literal, Protocol

from pydantic import JsonValue

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId
from vlah.ports.harness import HookConfig, ToolEndpoint

RecorderEventType = Literal[
    "blackboard_write", "blackboard_read", "fork", "allocation", "abandon"
]


class RecorderEvent(FrozenModel):
    """One line for the testbed recorder (Herd Testbed section 11.4)."""

    ts: float
    run_id: str
    arm: str
    actor: ActorId
    type: RecorderEventType
    data: dict[str, JsonValue]


class Recorder(Protocol):
    def post(self, events: tuple[RecorderEvent, ...]) -> None: ...


class ToolServer(Protocol):
    def endpoint_for(self, agent: ActorId) -> ToolEndpoint:
        """MCP URL plus a bearer token that identifies this agent."""
        ...

    def hook_config_for(self, agent: ActorId) -> HookConfig: ...

    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        """Claim ids the agent wrote during the task; at least one is required (D4)."""
        ...

    def serve(self) -> None: ...
