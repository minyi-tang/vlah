"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The Integrator: stacks verified candidates onto a moving baseline, one at a time,
around the testbed's shared merger (v2.2 D72, D74, D76, D79).
Implemented by W1-09 (#19).
"""

from typing import Protocol

from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import Event


class IntegratorService(Protocol):
    def on_verified(self, result_claim_id: str) -> None:
        """Queue a result claim verified at the top level, first in, first out."""
        ...

    def tick(self) -> tuple[Event, ...]: ...

    def current(self) -> BaselineRecord: ...

    def closeout(self) -> BaselineRecord:
        """Run the D79 closeout order and return the frozen deliverable."""
        ...
