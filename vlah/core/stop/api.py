"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

Stop rules (v2.2 D52). Implemented by W1-10 (#20).
"""

from typing import Protocol

from vlah.core.types.events import Event


class StopRules(Protocol):
    def evaluate(self, now_seconds: float) -> Event | None:
        """A stopped or escalation event when a rule fires, at most once per rule."""
        ...
