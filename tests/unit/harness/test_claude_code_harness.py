"""What the Claude Code harness asks Claude Code to do, and how it reads the transcript."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.worker import TASK, WORKER, assistant_line, retry_line
from vlah.adapters.harness.claude_code.harness import (
    MCP_CONFIG_PATH,
    RUNNER_MODULE,
    SETTINGS_PATH,
    ClaudeCodeConfig,
    ClaudeCodeHarness,
)
from vlah.core.types.params import Params
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import Handle, StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.testing.conformance import HarnessCheckInputs, check_harness

pytestmark = pytest.mark.harness

CONFIG = ClaudeCodeConfig(
    model="model-from-herd-yaml",
    context_window_tokens=200_000,
    python_executable="/venv/bin/python",
    environment={"ANTHROPIC_API_KEY": "from-the-runtime"},
)
ENDPOINT = ToolEndpoint(url="http://127.0.0.1:8100/mcp", bearer_token="agent-token")
HOOKS = HookConfig(
    interrupt_check_url="http://127.0.0.1:8100/hook/check",
    interrupt_ack_url="http://127.0.0.1:8100/hook/ack",
)
WORKSPACE = Workspace(
    target_repository="/repos/target",
    target_ref="main",
    blackboard_clone_url="file:///runs/r-1/blackboard.git",
)
START_PACK = StartPack(files={"CONTEXT.md": "## Your task\n\nGo faster."})


def harness() -> ClaudeCodeHarness:
    return ClaudeCodeHarness(config=CONFIG, params=Params())


def test_passes_the_harness_check() -> None:
    check_harness(
        harness(),
        HarnessCheckInputs(
            agent=WORKER,
            task=TASK,
            start_pack=START_PACK,
            tool_endpoint=ENDPOINT,
            hook_config=HOOKS,
            workspace=WORKSPACE,
        ),
    )
    with pytest.raises(NotSupportedError):
        harness().export_state(Handle(handle_id="h-1", agent=WORKER, task_id=TASK.id))


def test_build_runs_claude_headless_with_the_herd_server_and_the_hook() -> None:
    spec = harness().build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    command = list(spec.command)
    assert command[:3] == [
        "/venv/bin/python",
        "-m",
        "vlah.adapters.harness.claude_code.runner",
    ]
    claude = command[3:]
    assert claude[:2] == ["claude", "-p"]
    assert "Make the solver faster." in claude[2], "the prompt states the task's goal"

    def flag(name: str) -> str:
        return claude[claude.index(name) + 1]

    assert flag("--output-format") == "stream-json" and "--verbose" in claude
    assert flag("--model") == "model-from-herd-yaml"
    assert flag("--mcp-config") == MCP_CONFIG_PATH and "--strict-mcp-config" in claude
    assert flag("--settings") == SETTINGS_PATH
    assert flag("--permission-mode") == "dontAsk"
    assert "mcp__herd__*" in flag("--allowedTools").split(",")

    server = json.loads(spec.files[MCP_CONFIG_PATH])["mcpServers"]["herd"]
    assert server == {
        "type": "http",
        "url": ENDPOINT.url,
        "headers": {"Authorization": "Bearer ${VLAH_TOOL_TOKEN}"},
    }
    assert (
        "agent-token" not in spec.files[MCP_CONFIG_PATH]
    ), "the token stays in the env"
    settings = json.loads(spec.files[SETTINGS_PATH])
    (group,) = settings["hooks"]["PreToolUse"]
    assert group["matcher"] == "*"
    assert group["hooks"][0]["command"] == (
        "/venv/bin/python -m vlah.adapters.harness.claude_code.hook"
    )
    assert settings["autoCompactEnabled"] is False
    assert spec.env["DISABLE_AUTO_COMPACT"] == "1"
    assert spec.env["VLAH_TOOL_TOKEN"] == "agent-token"
    assert spec.env["VLAH_HOOK_CHECK_URL"] == HOOKS.interrupt_check_url
    assert spec.env["VLAH_CONTEXT_RESTART_FRACTION"] == "0.7"
    assert spec.env["ANTHROPIC_API_KEY"] == "from-the-runtime"
    assert spec.files["CONTEXT.md"] == START_PACK.files["CONTEXT.md"]


def test_usage_counts_the_latest_context_and_skips_broken_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        assistant_line(40_000, "Bash")
        + retry_line(429)
        + retry_line(529)
        + "{not json\n"
        + assistant_line(150_000, "mcp__herd__repo_write")
        + '{"type": "assistant", "message": {"content": [{"type": "text"}]}}\n'
    )
    usage = harness().usage(transcript)
    assert (usage.used_tokens, usage.window_tokens) == (150_000, 200_000)
    assert usage.fraction == 0.75
    calls = harness().recent_tool_calls(transcript, 1)
    assert calls == ('mcp__herd__repo_write {"command": "ls"}',)
    assert len(harness().recent_tool_calls(transcript, 20)) == 2
    assert harness().rate_limit_retries(transcript) == 1
    assert harness().usage(tmp_path / "missing.jsonl").used_tokens == 0


def test_the_runner_copies_the_stream_to_the_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / ".vlah" / "local-00001-1.transcript.jsonl"
    stand_in_for_claude = (
        sys.executable,
        "-c",
        'import sys; print(\'{"type": "assistant"}\'); sys.exit(3)',
    )
    completed = subprocess.run(
        [sys.executable, "-m", RUNNER_MODULE, *stand_in_for_claude],
        env={"PATH": os.environ.get("PATH", ""), "VLAH_TRANSCRIPT": str(transcript)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 3, "the runner exits with Claude Code's code"
    assert completed.stdout == '{"type": "assistant"}\n'
    assert transcript.read_text() == completed.stdout
