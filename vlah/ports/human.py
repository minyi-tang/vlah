"""Human port: the channel between a person and the herd's lead agent (v2.2 section 10)."""

from typing import Literal, Protocol

from vlah.core.types.base import FrozenModel


class HumanMessage(FrozenModel):
    sender: Literal["herd", "human"]
    text: str
    timestamp_seconds: float


class Human(Protocol):
    def send(self, message: HumanMessage) -> None: ...

    def receive(self, timeout_seconds: float) -> HumanMessage | None:
        """The next message from the person, or None at the timeout."""
        ...
