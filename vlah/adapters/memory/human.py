"""An in-process human channel for tests: queued replies and a record of what was sent."""

from collections import deque

from vlah.ports.human import HumanMessage


class MemoryHuman:
    def __init__(self) -> None:
        self.sent: list[HumanMessage] = []
        self.queued_replies: deque[HumanMessage] = deque()

    def send(self, message: HumanMessage) -> None:
        self.sent.append(message)

    def queue_reply(self, message: HumanMessage) -> None:
        """Test hook: the next message the person 'says'."""
        self.queued_replies.append(message)

    def receive(self, timeout_seconds: float) -> HumanMessage | None:
        if self.queued_replies:
            return self.queued_replies.popleft()
        else:
            return None
