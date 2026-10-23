"""Harness port: which agent program does the work (v2.2 section 4.1, D60, D77)."""

from pathlib import Path
from typing import Protocol

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.ports.executor import AgentSpec, Handle, StartPack


class ToolEndpoint(FrozenModel):
    """Where the agent reaches the herd's MCP tool server, and its bearer token."""

    url: str
    bearer_token: str


class HookConfig(FrozenModel):
    """Endpoints the harness's tool hook calls to check and acknowledge interrupts."""

    interrupt_check_url: str
    interrupt_ack_url: str


class Workspace(FrozenModel):
    """The repositories a session works in: the target system and the blackboard."""

    target_repository: str
    target_ref: str
    blackboard_clone_url: str


class ContextUsage(FrozenModel):
    used_tokens: int
    window_tokens: int

    @property
    def fraction(self) -> float:
        return self.used_tokens / self.window_tokens


class Harness(Protocol):
    @property
    def supports_tool_hooks(self) -> bool:
        """True if every tool call, not only herd tools, can be checked for interrupts."""
        ...

    @property
    def supports_checkpoint(self) -> bool:
        """True if a conversation can be exported and resumed elsewhere."""
        ...

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec: ...

    def usage(self, transcript_path: Path) -> ContextUsage: ...

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        """The last count tool calls in a transcript, one line each, for the D77 pack."""
        ...

    def rate_limit_retries(self, transcript_path: Path) -> int:
        """How often the model API throttled the session (C7)."""
        ...

    def export_state(self, handle: Handle) -> bytes:
        """Raises NotSupportedError when supports_checkpoint is False."""
        ...

    def import_state(self, state_blob: bytes) -> AgentSpec:
        """Raises NotSupportedError when supports_checkpoint is False."""
        ...
