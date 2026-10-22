"""Model port: the LLM behind each role, plus embeddings (v2.2 section 4.1, P10)."""

from typing import Literal, Protocol

from pydantic import JsonValue

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import Role


class ChatMessage(FrozenModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ToolDefinition(FrozenModel):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


class ToolCall(FrozenModel):
    name: str
    arguments: dict[str, JsonValue]


class ModelReply(FrozenModel):
    content: str
    tool_calls: tuple[ToolCall, ...] = ()


class Model(Protocol):
    def complete(
        self,
        role: Role,
        messages: tuple[ChatMessage, ...],
        tools: tuple[ToolDefinition, ...],
    ) -> ModelReply:
        """Every role gets the strongest model; only the routing table may say otherwise."""
        ...

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...
