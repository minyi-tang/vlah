"""Claude Code, headless, as a worker (v2.2 section 7, D60 layer 2, D77, P10).

The AgentSpec runs, from the sandbox root:

    python -m vlah.adapters.harness.claude_code.runner claude -p <prompt>
        --output-format stream-json --verbose --model <model>
        --mcp-config .vlah/claude/mcp.json --strict-mcp-config
        --settings .vlah/claude/settings.json
        --permission-mode dontAsk --allowedTools <tools>

with these files written into the sandbox:
- CONTEXT.md and the rest of the start pack;
- .vlah/claude/mcp.json: the herd server over streamable HTTP, whose bearer token
  is expanded from VLAH_TOOL_TOKEN, so the token is never written to a file;
- .vlah/claude/settings.json: the PreToolUse hook on every tool, and automatic
  compaction off (also DISABLE_AUTO_COMPACT=1).

Flags and keys follow the Claude Code docs as of 2.1.304. The model id and its
context window come from configuration and are never hard-coded (P10).
"""

import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.harness.claude_code.hook import HERD_SERVER_NAME
from vlah.adapters.harness.claude_code.transcript import (
    context_tokens,
    rate_limit_retries,
    tool_calls,
)
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, Handle, StartPack
from vlah.ports.harness import ContextUsage, HookConfig, ToolEndpoint, Workspace
from vlah.prompts.worker import render_worker_prompt

CONFIG_DIRECTORY = ".vlah/claude"
MCP_CONFIG_PATH = f"{CONFIG_DIRECTORY}/mcp.json"
SETTINGS_PATH = f"{CONFIG_DIRECTORY}/settings.json"
RUNNER_MODULE = "vlah.adapters.harness.claude_code.runner"
HOOK_MODULE = "vlah.adapters.harness.claude_code.hook"
HOOK_TIMEOUT_SECONDS = 10
# The docs reserve bypassPermissions for isolated containers and VMs. Until the
# sandbox enforces isolation (#22), dontAsk runs exactly the allowed tools and
# denies anything that would prompt.
DEFAULT_PERMISSION_MODE = "dontAsk"
DEFAULT_ALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    f"mcp__{HERD_SERVER_NAME}__*",
)
# Claude Code retries 429s and 529s itself with exponential backoff; the watchdog
# keeps retrying for an unattended run instead of failing the session (C7).
CLAUDE_ENVIRONMENT = {
    "DISABLE_AUTO_COMPACT": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_MAX_RETRIES": "15",
    "CLAUDE_CODE_RETRY_WATCHDOG": "1",
}


@dataclass(frozen=True, kw_only=True)
class ClaudeCodeConfig:
    """From herd.yaml: the model, its window, and what the worker process needs."""

    model: str
    context_window_tokens: int
    claude_command: tuple[str, ...] = ("claude",)
    python_executable: str = field(default_factory=lambda: sys.executable)
    permission_mode: str = DEFAULT_PERMISSION_MODE
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    # Passed through to every worker, for example the model API key.
    environment: dict[str, str] = field(default_factory=dict[str, str])


def mcp_config(tool_endpoint: ToolEndpoint) -> str:
    server = {
        "type": "http",
        "url": tool_endpoint.url,
        "headers": {"Authorization": "Bearer ${VLAH_TOOL_TOKEN}"},
    }
    return json.dumps({"mcpServers": {HERD_SERVER_NAME: server}}, indent=2)


def settings(python_executable: str) -> str:
    hook = {
        "type": "command",
        "command": shlex.join([python_executable, "-m", HOOK_MODULE]),
        "timeout": HOOK_TIMEOUT_SECONDS,
    }
    return json.dumps(
        {
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]},
            "autoCompactEnabled": False,
        },
        indent=2,
    )


class ClaudeCodeHarness:
    def __init__(self, *, config: ClaudeCodeConfig, params: Params) -> None:
        self.config = config
        self.params = params

    @property
    def supports_tool_hooks(self) -> bool:
        return True

    @property
    def supports_checkpoint(self) -> bool:
        return False

    def command(self, prompt: str) -> tuple[str, ...]:
        config = self.config
        return (
            config.python_executable,
            "-m",
            RUNNER_MODULE,
            *config.claude_command,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            config.model,
            "--mcp-config",
            MCP_CONFIG_PATH,
            "--strict-mcp-config",
            "--settings",
            SETTINGS_PATH,
            "--permission-mode",
            config.permission_mode,
            "--allowedTools",
            ",".join(config.allowed_tools),
        )

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        return AgentSpec(
            agent=agent,
            command=self.command(render_worker_prompt(agent, task)),
            files={
                **start_pack.files,
                MCP_CONFIG_PATH: mcp_config(tool_endpoint),
                SETTINGS_PATH: settings(self.config.python_executable),
            },
            env={
                **self.config.environment,
                **CLAUDE_ENVIRONMENT,
                "VLAH_TOOL_TOKEN": tool_endpoint.bearer_token,
                "VLAH_HOOK_CHECK_URL": hook_config.interrupt_check_url,
                "VLAH_HOOK_ACK_URL": hook_config.interrupt_ack_url,
                "VLAH_CONTEXT_WINDOW_TOKENS": str(self.config.context_window_tokens),
                "VLAH_CONTEXT_RESTART_FRACTION": str(
                    self.params.context_restart_fraction
                ),
            },
            target_repository=workspace.target_repository,
            target_ref=workspace.target_ref,
            blackboard_clone_url=workspace.blackboard_clone_url,
        )

    def usage(self, transcript_path: Path) -> ContextUsage:
        return ContextUsage(
            used_tokens=context_tokens(transcript_path),
            window_tokens=self.config.context_window_tokens,
        )

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        calls = tool_calls(transcript_path)
        return tuple(calls[-count:]) if count > 0 else ()

    def rate_limit_retries(self, transcript_path: Path) -> int:
        """How often the session was throttled by 429s, a diagnostic (C7, section 13)."""
        return rate_limit_retries(transcript_path)

    def export_state(self, handle: Handle) -> bytes:
        raise NotSupportedError("conversation checkpoints wait for the H11 spike (#24)")

    def import_state(self, state_blob: bytes) -> AgentSpec:
        raise NotSupportedError("conversation checkpoints wait for the H11 spike (#24)")
