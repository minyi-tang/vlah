"""A Harness whose agent is the scripted worker program (D61, H12).

With it a whole herd runs without a model through any executor: the loop builds
the session as for a model, the executor starts it, and the program speaks to the
tool server over MCP. The transcript is the program's ScriptedTranscript, so usage
and recent tool calls read as for a model.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.harness.egress import session_policy
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, Handle, StartPack
from vlah.ports.harness import ContextUsage, HookConfig, ToolEndpoint, Workspace
from vlah.testing.scripted_worker import (
    APPROACH_VARIABLE,
    DEFAULT_WINDOW_TOKENS,
    MODULE,
    SCRIPT_VARIABLE,
    SOURCES_VARIABLE,
    TASK_KIND_VARIABLE,
    TASK_REFS_VARIABLE,
    TOOL_TOKEN_VARIABLE,
    TOOL_URL_VARIABLE,
    WINDOW_TOKENS_VARIABLE,
    Script,
    read_transcript,
)


@dataclass(frozen=True, kw_only=True)
class ScriptedWorkerConfig:
    """source_ids are the judge levels the program predicts a score for."""

    source_ids: tuple[str, ...]
    script: Script = "submit_one"
    python_executable: str = field(default_factory=lambda: sys.executable)
    window_tokens: int = DEFAULT_WINDOW_TOKENS
    environment: dict[str, str] = field(default_factory=dict[str, str])


class ScriptedWorkerHarness:
    def __init__(self, *, config: ScriptedWorkerConfig) -> None:
        self.config = config

    @property
    def supports_tool_hooks(self) -> bool:
        return False

    @property
    def supports_checkpoint(self) -> bool:
        return False

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        config = self.config
        return AgentSpec(
            agent=agent,
            command=(
                config.python_executable,
                "-m",
                MODULE,
                "--script",
                config.script,
            ),
            files=start_pack.files,
            env={
                **config.environment,
                TOOL_URL_VARIABLE: tool_endpoint.url,
                TOOL_TOKEN_VARIABLE: tool_endpoint.bearer_token,
                SCRIPT_VARIABLE: config.script,
                APPROACH_VARIABLE: task.approach,
                TASK_KIND_VARIABLE: task.kind,
                TASK_REFS_VARIABLE: ",".join(task.refs),
                SOURCES_VARIABLE: ",".join(config.source_ids),
                WINDOW_TOKENS_VARIABLE: str(config.window_tokens),
            },
            target_repository=workspace.target_repository,
            target_ref=workspace.target_ref,
            blackboard_clone_url=workspace.blackboard_clone_url,
            egress=session_policy(tool_endpoint, hook_config, (), ()),
        )

    def usage(self, transcript_path: Path) -> ContextUsage:
        transcript = read_transcript(transcript_path)
        return ContextUsage(
            used_tokens=transcript.used_tokens, window_tokens=transcript.window_tokens
        )

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        calls = read_transcript(transcript_path).tool_calls
        return calls[-count:] if count > 0 else ()

    def rate_limit_retries(self, transcript_path: Path) -> int:
        return 0

    def export_state(self, handle: Handle) -> bytes:
        raise NotSupportedError("the scripted worker has no conversation to export")

    def import_state(self, state_blob: bytes) -> AgentSpec:
        raise NotSupportedError("the scripted worker has no conversation to import")
