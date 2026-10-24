"""The Human port on a terminal: messages print, replies are typed lines (v2.2 section 10).

vlah inbox sends each open escalation through it. Replies are read for the Wave 3
vlah answer; output and replies can be any text streams, stdout and stdin or files.
"""

import select
import time
from dataclasses import dataclass
from typing import TextIO

from vlah.core.types.clock import Clock
from vlah.ports.human import HumanMessage


@dataclass(frozen=True, kw_only=True)
class CliHuman:
    """ports.human.Human over two text streams."""

    output: TextIO
    replies: TextIO
    clock: Clock

    def send(self, message: HumanMessage) -> None:
        written = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(message.timestamp_seconds)
        )
        print(f"[{written}] {message.text}\n", file=self.output, flush=True)

    def receive(self, timeout_seconds: float) -> HumanMessage | None:
        """The next line typed, or None at the timeout or at the end of input."""
        ready_streams, _, _ = select.select([self.replies], [], [], timeout_seconds)
        line = self.replies.readline() if ready_streams else ""
        if not line:
            return None
        else:
            return HumanMessage(
                sender="human",
                text=line.rstrip("\n"),
                timestamp_seconds=self.clock.now_seconds(),
            )
