"""A deterministic Model: hashed bag-of-words embeddings and scripted replies (D61)."""

import hashlib
import math
import re
from collections import deque

from vlah.core.types.ids import Role
from vlah.ports.model import ChatMessage, ModelReply, ToolDefinition

EMBEDDING_DIMENSIONS = 256
WORD_PATTERN = re.compile(r"\w+")


def hashed_bag_of_words(text: str) -> tuple[float, ...]:
    """Unit-length vector where each lowercase word adds plus or minus one to a hashed slot."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for word in WORD_PATTERN.findall(text.lower()):
        word_digest = hashlib.sha256(word.encode("utf-8")).digest()
        slot = int.from_bytes(word_digest[:4], "big") % EMBEDDING_DIMENSIONS
        if word_digest[4] % 2 == 0:
            vector[slot] += 1.0
        else:
            vector[slot] -= 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return tuple(vector)
    else:
        return tuple(value / norm for value in vector)


class MemoryModel:
    """complete returns the queued replies in order and fails loudly when they run out."""

    def __init__(self, *, scripted_replies: tuple[ModelReply, ...] = ()) -> None:
        self.scripted_replies: deque[ModelReply] = deque(scripted_replies)
        self.requests: list[tuple[Role, tuple[ChatMessage, ...]]] = []

    def complete(
        self,
        role: Role,
        messages: tuple[ChatMessage, ...],
        tools: tuple[ToolDefinition, ...],
    ) -> ModelReply:
        self.requests.append((role, messages))
        if self.scripted_replies:
            return self.scripted_replies.popleft()
        else:
            raise RuntimeError(f"no scripted reply left for the {role} request")

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(hashed_bag_of_words(text) for text in texts)
