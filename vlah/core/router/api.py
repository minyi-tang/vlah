"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The router: decides which merged events must reach a worker now (v2.2 section 8,
D17, D29). Implemented by W1-05 (#15).
"""

from typing import Protocol

from vlah.core.blackboard.api import MergedCommit
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt


class Router(Protocol):
    def on_commit(self, merged_commit: MergedCommit) -> tuple[Interrupt, ...]:
        """Evaluate the rules against every active lease and record the interrupts."""
        ...

    def check(self, agent: ActorId) -> Interrupt | None:
        """The pending hard interrupt for agent; one key-value read, no scans."""
        ...

    def acknowledge(self, agent: ActorId, interrupt_id: str) -> None: ...

    def overdue(self, now_seconds: float) -> tuple[Interrupt, ...]:
        """Hard interrupts unacknowledged past the timeout; the executor then restarts the agent."""
        ...
