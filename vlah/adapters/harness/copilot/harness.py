"""GitHub Copilot CLI, non-interactive, as a worker (v2.2 section 7, D60 layer 2, D77, P10).

The AgentSpec runs, from the sandbox root:

    python -m vlah.adapters.harness.copilot.runner copilot -p <prompt>
        --output-format json --allow-all-tools --no-ask-user --no-auto-update
        --disable-builtin-mcps --no-custom-instructions
        --additional-mcp-config @.vlah/copilot/herd-mcp.json --model <model> ...

with these files written into the sandbox, under the worker's private COPILOT_HOME
(.vlah/copilot, set by the runner):
- settings.json: the preToolUse hook on every tool, as a user-level hook, so it
  loads without trusting the sandbox folder;
- herd-mcp.json: the herd's MCP server over streamable HTTP with the agent's token.

Flags and keys follow Copilot CLI 1.0.102 and were checked by running it. File
access stays inside the working directory (the sandbox), which is Copilot's default.
Copilot compacts the conversation itself at 80% of the window and the CLI cannot
turn that off; the hook asks the agent to wrap up at context_restart_fraction (70%)
first. The model comes from configuration, but an administrator-managed setting can
override it; the transcript records the model actually used.
"""

import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.harness.copilot.transcript import (
    latest_context,
    rate_limit_events,
    tool_calls,
)
from vlah.adapters.harness.interrupt_hook import HERD_SERVER_NAME, WRAP_UP_TOOL_NAMES
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, Handle, StartPack
from vlah.ports.harness import ContextUsage, HookConfig, ToolEndpoint, Workspace
from vlah.prompts.worker import render_worker_prompt

HOME_DIRECTORY = ".vlah/copilot"
SETTINGS_PATH = f"{HOME_DIRECTORY}/settings.json"
MCP_CONFIG_PATH = f"{HOME_DIRECTORY}/herd-mcp.json"
RUNNER_MODULE = "vlah.adapters.harness.copilot.runner"
HOOK_MODULE = "vlah.adapters.harness.copilot.hook"
HOOK_TIMEOUT_SECONDS = 10
# Copilot names an MCP tool <server>-<tool>.
WRAP_UP_TOOLS = tuple(f"{HERD_SERVER_NAME}-{name}" for name in WRAP_UP_TOOL_NAMES)
COPILOT_ENVIRONMENT = {"COPILOT_AUTO_UPDATE": "false", "NO_COLOR": "1"}


@dataclass(frozen=True, kw_only=True)
class CopilotConfig:
    """From herd.yaml: the model, and what the worker process needs."""

    model: str
    reasoning_effort: str | None = None
    # Used only when Copilot's usage records do not say the window.
    context_window_tokens: int | None = None
    copilot_command: tuple[str, ...] = ("copilot",)
    python_executable: str = field(default_factory=lambda: sys.executable)
    # Hosts the agent's shell may reach, such as the testbed's slow tier for tb fast.
    allowed_urls: tuple[str, ...] = ()
    # Passed through to every worker.
    environment: dict[str, str] = field(default_factory=dict[str, str])


def mcp_config(tool_endpoint: ToolEndpoint) -> str:
    server = {
        "type": "http",
        "url": tool_endpoint.url,
        "headers": {"Authorization": f"Bearer {tool_endpoint.bearer_token}"},
        "tools": ["*"],
    }
    return json.dumps({"mcpServers": {HERD_SERVER_NAME: server}}, indent=2)


def settings(python_executable: str) -> str:
    hook = {
        "type": "command",
        "bash": shlex.join([python_executable, "-m", HOOK_MODULE]),
        "timeoutSec": HOOK_TIMEOUT_SECONDS,
    }
    return json.dumps({"hooks": {"preToolUse": [hook]}}, indent=2)


class CopilotHarness:
    def __init__(self, *, config: CopilotConfig, params: Params) -> None:
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
        effort = (
            ("--reasoning-effort", config.reasoning_effort)
            if config.reasoning_effort
            else ()
        )
        urls = tuple(
            argument for url in config.allowed_urls for argument in ("--allow-url", url)
        )
        return (
            config.python_executable,
            "-m",
            RUNNER_MODULE,
            *config.copilot_command,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allow-all-tools",
            "--no-ask-user",
            "--no-auto-update",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--additional-mcp-config",
            f"@{MCP_CONFIG_PATH}",
            "--model",
            config.model,
            *effort,
            *urls,
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
        window = self.config.context_window_tokens
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
                **COPILOT_ENVIRONMENT,
                "VLAH_TOOL_TOKEN": tool_endpoint.bearer_token,
                "VLAH_HOOK_CHECK_URL": hook_config.interrupt_check_url,
                "VLAH_HOOK_ACK_URL": hook_config.interrupt_ack_url,
                "VLAH_CONTEXT_RESTART_FRACTION": str(
                    self.params.context_restart_fraction
                ),
                "VLAH_WRAP_UP_TOOLS": ",".join(WRAP_UP_TOOLS),
                **({"VLAH_CONTEXT_WINDOW_TOKENS": str(window)} if window else {}),
            },
            target_repository=workspace.target_repository,
            target_ref=workspace.target_ref,
            blackboard_clone_url=workspace.blackboard_clone_url,
        )

    def usage(self, transcript_path: Path) -> ContextUsage:
        reading = latest_context(transcript_path)
        window = reading.window_tokens or self.config.context_window_tokens
        if window is None and reading.used_tokens == 0:
            # Before the first model call nothing is used, whatever the window.
            return ContextUsage(used_tokens=0, window_tokens=1)
        elif window is None:
            raise ValueError(
                f"{transcript_path} does not say the context window; set "
                "context_window_tokens"
            )
        else:
            return ContextUsage(used_tokens=reading.used_tokens, window_tokens=window)

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        calls = tool_calls(transcript_path)
        return tuple(calls[-count:]) if count > 0 else ()

    def rate_limit_retries(self, transcript_path: Path) -> int:
        return rate_limit_events(transcript_path)

    def export_state(self, handle: Handle) -> bytes:
        raise NotSupportedError("conversation checkpoints wait for the H11 spike (#24)")

    def import_state(self, state_blob: bytes) -> AgentSpec:
        raise NotSupportedError("conversation checkpoints wait for the H11 spike (#24)")
