"""Interrupts the router raises for a running worker (v2.2 section 8, D17)."""

from typing import Literal

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId


class Interrupt(FrozenModel):
    """A routed event for one agent; hard ones stop the current tool call."""

    id: str
    agent: ActorId
    kind: Literal["hard", "soft"]
    rule: int
    reason: str
    refs: tuple[str, ...] = ()
    written_ts: float
