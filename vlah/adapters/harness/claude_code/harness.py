"""Claude Code, headless, as a worker (v2.2 section 7, D60 layer 2, D77, P10).

The AgentSpec runs, from the sandbox root:

    python -m vlah.adapters.harness.claude_code.runner <claude command> -p <prompt>
        --output-format stream-json --verbose --model <model> [--effort <level>]
        --mcp-config .vlah/claude/mcp.json --strict-mcp-config
        --settings .vlah/claude/settings.json
        --permission-mode dontAsk --allowedTools <tools>

with these files written into the sandbox:
- CONTEXT.md and the rest of the start pack;
- .vlah/claude/mcp.json: the herd's MCP server over streamable HTTP with the
  agent's token, and no other server (--strict-mcp-config);
- .vlah/claude/settings.json: the PreToolUse hook on every tool.

The runner gives Claude Code a private config directory, so the user's own login,
settings and MCP servers never load; the model is reached with what the
configuration passes in the environment. The command can be a wrapper, such as a
script that points Claude Code at a LiteLLM gateway in front of GitHub Copilot.
Automatic compaction is off (DISABLE_AUTO_COMPACT): the hook asks the agent to
wrap up at context_restart_fraction and the loop restarts it from the D77 pack.

The egress policy (D75) lists the herd's tool server and hook endpoints, the
configured allowed_urls (hosts the agent's shell may reach) and the model API
(model_api_urls, api.anthropic.com unless a gateway is configured). Flags follow
Claude Code 2.1.308 and were checked by running it.
"""

import json
import logging
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.harness.claude_code.transcript import (
    context_tokens,
    models,
    rate_limit_retries,
    tool_calls,
)
from vlah.adapters.harness.egress import session_policy, url_rules
from vlah.adapters.harness.interrupt_hook import HERD_SERVER_NAME, WRAP_UP_TOOL_NAMES
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy, Params
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, Handle, StartPack
from vlah.ports.harness import ContextUsage, HookConfig, ToolEndpoint, Workspace
from vlah.prompts.worker import render_worker_prompt

logger = logging.getLogger(__name__)

CONFIG_DIRECTORY = ".vlah/claude"
MCP_CONFIG_PATH = f"{CONFIG_DIRECTORY}/mcp.json"
SETTINGS_PATH = f"{CONFIG_DIRECTORY}/settings.json"
RUNNER_MODULE = "vlah.adapters.harness.claude_code.runner"
HOOK_MODULE = "vlah.adapters.harness.claude_code.hook"
HOOK_TIMEOUT_SECONDS = 10
# dontAsk runs exactly the allowed tools and denies anything that would prompt.
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
DEFAULT_MODEL_API_URLS = ("https://api.anthropic.com",)
WRAP_UP_TOOLS = tuple(f"mcp__{HERD_SERVER_NAME}__{name}" for name in WRAP_UP_TOOL_NAMES)
# Claude Code retries 429s and 529s itself with exponential backoff; the watchdog
# keeps retrying for an unattended run instead of failing the session (C7).
CLAUDE_ENVIRONMENT = {
    "DISABLE_AUTO_COMPACT": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_MAX_RETRIES": "15",
    "CLAUDE_CODE_RETRY_WATCHDOG": "1",
}


@dataclass(frozen=True, kw_only=True)
class ClaudeCodeConfig:
    """From herd.yaml: the model, its window, and what the worker process needs."""

    model: str
    context_window_tokens: int
    effort: str | None = None
    claude_command: tuple[str, ...] = ("claude",)
    python_executable: str = field(default_factory=lambda: sys.executable)
    permission_mode: str = DEFAULT_PERMISSION_MODE
    allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    # Hosts the agent's shell may reach, such as a local judge for quick checks.
    allowed_urls: tuple[str, ...] = ()
    # Where Claude Code reaches the model: the Anthropic API, or a gateway.
    model_api_urls: tuple[str, ...] = DEFAULT_MODEL_API_URLS
    # Passed through to every worker, for example CLAUDE_BIN for a wrapper script.
    environment: dict[str, str] = field(default_factory=dict[str, str])


def mcp_config(tool_endpoint: ToolEndpoint) -> str:
    server = {
        "type": "http",
        "url": tool_endpoint.url,
        "headers": {"Authorization": f"Bearer {tool_endpoint.bearer_token}"},
    }
    return json.dumps({"mcpServers": {HERD_SERVER_NAME: server}}, indent=2)


def settings(python_executable: str) -> str:
    hook = {
        "type": "command",
        "command": shlex.join([python_executable, "-m", HOOK_MODULE]),
        "timeout": HOOK_TIMEOUT_SECONDS,
    }
    return json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}}, indent=2
    )


def egress_policy(
    config: ClaudeCodeConfig, tool_endpoint: ToolEndpoint, hook_config: HookConfig
) -> EgressPolicy:
    return session_policy(
        tool_endpoint,
        hook_config,
        config.allowed_urls,
        url_rules(config.model_api_urls, "model_api"),
    )


class ClaudeCodeHarness:
    def __init__(self, *, config: ClaudeCodeConfig, params: Params) -> None:
        self.config = config
        self.params = params
        self.models_logged: set[Path] = set()

    @property
    def supports_tool_hooks(self) -> bool:
        return True

    @property
    def supports_checkpoint(self) -> bool:
        return False

    def command(self, prompt: str) -> tuple[str, ...]:
        config = self.config
        effort = ("--effort", config.effort) if config.effort else ()
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
            *effort,
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
                "VLAH_WRAP_UP_TOOLS": ",".join(WRAP_UP_TOOLS),
            },
            target_repository=workspace.target_repository,
            target_ref=workspace.target_ref,
            blackboard_clone_url=workspace.blackboard_clone_url,
            egress=egress_policy(self.config, tool_endpoint, hook_config),
        )

    def log_models(self, transcript_path: Path) -> None:
        """The model each session ran on, once per session: the user's configuration."""
        if transcript_path in self.models_logged:
            return
        else:
            ran = models(transcript_path)
            if ran:
                self.models_logged.add(transcript_path)
                logger.info(f"{transcript_path}: Claude Code ran {', '.join(ran)}")
            else:
                pass

    def usage(self, transcript_path: Path) -> ContextUsage:
        self.log_models(transcript_path)
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
