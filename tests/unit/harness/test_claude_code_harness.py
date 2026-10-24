"""What the Claude Code harness asks Claude Code to do, its runner and hook, and how it
reads the transcript."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.worker import TASK, WORKER
from vlah.adapters.harness.claude_code import hook
from vlah.adapters.harness.claude_code.harness import (
    MCP_CONFIG_PATH,
    RUNNER_MODULE,
    SETTINGS_PATH,
    WRAP_UP_TOOLS,
    ClaudeCodeConfig,
    ClaudeCodeHarness,
)
from vlah.adapters.harness.claude_code.runner import HOME_DIRECTORY
from vlah.core.types.params import EgressPolicy, HostRule, Params
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import Handle, StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.testing.conformance import HarnessCheckInputs, check_harness

pytestmark = pytest.mark.harness

WINDOW_TOKENS = 200_000
CONFIG = ClaudeCodeConfig(
    model="claude-opus-5-5",
    context_window_tokens=WINDOW_TOKENS,
    effort="low",
    claude_command=("/home/me/litellm-copilot/claude-ghcp",),
    python_executable="/venv/bin/python",
    model_api_urls=("http://127.0.0.1:4000",),
    environment={"CLAUDE_BIN": "/apps/claude"},
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


def harness(config: ClaudeCodeConfig = CONFIG) -> ClaudeCodeHarness:
    return ClaudeCodeHarness(config=config, params=Params())


def assistant_line(
    context_tokens: int, tool_name: str | None = None, model: str = "claude-opus-5-5"
) -> str:
    """One stream-json assistant message whose request used this many context tokens."""
    content: list[dict[str, object]] = [{"type": "text", "text": "thinking"}]
    if tool_name is not None:
        content.append(
            {"type": "tool_use", "name": tool_name, "input": {"command": "ls"}}
        )
    else:
        pass
    message = {
        "model": model,
        "content": content,
        "usage": {
            "input_tokens": context_tokens - 30,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 10,
            "output_tokens": 7,
        },
    }
    return json.dumps({"type": "assistant", "message": message}) + "\n"


def retry_line(status: int) -> str:
    return (
        json.dumps({"type": "system", "subtype": "api_retry", "error_status": status})
        + "\n"
    )


def test_passes_the_harness_check(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    check_harness(
        harness(),
        HarnessCheckInputs(
            agent=WORKER,
            task=TASK,
            start_pack=START_PACK,
            tool_endpoint=ENDPOINT,
            hook_config=HOOKS,
            workspace=WORKSPACE,
            transcript_path=transcript,
        ),
    )


def test_build_runs_claude_code_through_the_wrapper_with_the_herd_server_and_hook() -> (
    None
):
    spec = harness().build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    command = spec.command
    assert command[:4] == (
        "/venv/bin/python",
        "-m",
        RUNNER_MODULE,
        "/home/me/litellm-copilot/claude-ghcp",
    )
    flags = dict(zip(command[5::1], command[6::1], strict=False))
    assert flags["--output-format"] == "stream-json"
    assert flags["--model"] == "claude-opus-5-5"
    assert flags["--effort"] == "low"
    assert flags["--mcp-config"] == MCP_CONFIG_PATH
    assert flags["--settings"] == SETTINGS_PATH
    assert flags["--permission-mode"] == "dontAsk"
    assert "mcp__herd__*" in flags["--allowedTools"].split(",")
    assert "--strict-mcp-config" in command
    assert TASK.goal in command[command.index("-p") + 1]
    server = json.loads(spec.files[MCP_CONFIG_PATH])["mcpServers"]["herd"]
    assert server == {
        "type": "http",
        "url": ENDPOINT.url,
        "headers": {"Authorization": "Bearer agent-token"},
    }
    hooks = json.loads(spec.files[SETTINGS_PATH])["hooks"]["PreToolUse"]
    assert hooks[0]["matcher"] == "*"
    assert "vlah.adapters.harness.claude_code.hook" in hooks[0]["hooks"][0]["command"]
    assert spec.files["CONTEXT.md"] == START_PACK.files["CONTEXT.md"]
    assert spec.env["CLAUDE_BIN"] == "/apps/claude"
    assert spec.env["DISABLE_AUTO_COMPACT"] == "1"
    assert spec.env["VLAH_TOOL_TOKEN"] == "agent-token"
    assert spec.env["VLAH_CONTEXT_WINDOW_TOKENS"] == str(WINDOW_TOKENS)
    assert spec.env["VLAH_WRAP_UP_TOOLS"].split(",") == list(WRAP_UP_TOOLS)


def test_build_allows_the_herd_tools_and_the_model_gateway_only() -> None:
    spec = harness().build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    assert spec.egress == EgressPolicy(
        allow=(
            HostRule(host="127.0.0.1", ports=(8100,), purpose="tools"),
            HostRule(host="127.0.0.1", ports=(4000,), purpose="model_api"),
        )
    )


def test_without_a_gateway_the_model_api_is_anthropic() -> None:
    direct = ClaudeCodeConfig(
        model="claude-opus-5-5", context_window_tokens=WINDOW_TOKENS
    )
    spec = harness(direct).build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    assert spec.egress is not None
    assert HostRule(host="api.anthropic.com", purpose="model_api") in spec.egress.allow
    assert "--effort" not in spec.command


def test_usage_tool_calls_and_retries_come_from_the_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        assistant_line(10_000, "Bash")
        + retry_line(429)
        + retry_line(529)
        + "not json, cut off mid-wr"
        + "\n"
        + assistant_line(50_000, "mcp__herd__repo_write")
    )
    usage = harness().usage(transcript)
    assert usage.used_tokens == 50_000 and usage.window_tokens == WINDOW_TOKENS
    calls = harness().recent_tool_calls(transcript, 1)
    assert calls == ('mcp__herd__repo_write {"command": "ls"}',)
    assert harness().rate_limit_retries(transcript) == 1
    assert harness().usage(tmp_path / "missing.jsonl").used_tokens == 0


def test_checkpoints_are_not_supported() -> None:
    with pytest.raises(NotSupportedError):
        harness().export_state(Handle(handle_id="h", agent=WORKER, task_id=TASK.id))


def test_the_hook_denies_in_claude_codes_format_past_the_context_threshold(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(assistant_line(int(WINDOW_TOKENS * 0.8)))
    environment = {
        "VLAH_HOOK_CHECK_URL": "http://127.0.0.1:1/hook/check",
        "VLAH_HOOK_ACK_URL": "http://127.0.0.1:1/hook/ack",
        "VLAH_TOOL_TOKEN": "t",
        "VLAH_TRANSCRIPT": str(transcript),
        "VLAH_CONTEXT_WINDOW_TOKENS": str(WINDOW_TOKENS),
        "VLAH_CONTEXT_RESTART_FRACTION": "0.7",
        "VLAH_WRAP_UP_TOOLS": ",".join(WRAP_UP_TOOLS),
    }
    stdout = io.StringIO()
    code = hook.main(
        io.StringIO(json.dumps({"tool_name": "Bash"})), stdout, environment
    )
    decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
    assert code == 0
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "80% full" in decision["permissionDecisionReason"]
    wrap_up = io.StringIO()
    payload = json.dumps({"tool_name": "mcp__herd__repo_write"})
    assert hook.main(io.StringIO(payload), wrap_up, environment) == 0
    assert wrap_up.getvalue() == "", "the wrap-up tools stay allowed"
    broken = io.StringIO()
    assert hook.main(io.StringIO("not json"), broken, environment) == 0
    assert broken.getvalue() == "", "a broken hook lets the call through"


def test_the_runner_gives_claude_code_a_private_home_and_copies_its_output(
    tmp_path: Path,
) -> None:
    fake_claude = tmp_path / "fake_claude.py"
    fake_claude.write_text(
        "import os, json\n"
        "print(json.dumps({'type': 'system', 'home': os.environ['CLAUDE_CONFIG_DIR']}))\n"
        "print(json.dumps({'type': 'result', 'result': 'done'}))\n"
    )
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    transcript = sandbox / ".vlah" / "s-1.transcript.jsonl"
    completed = subprocess.run(
        [sys.executable, "-m", RUNNER_MODULE, sys.executable, str(fake_claude)],
        cwd=sandbox,
        env={**os.environ, "VLAH_TRANSCRIPT": str(transcript)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert lines[0]["home"] == str((sandbox / HOME_DIRECTORY).resolve())
    assert lines[1]["result"] == "done"
    assert completed.stdout.splitlines()[1] == json.dumps(
        {"type": "result", "result": "done"}
    ), "the output still reaches the executor's log"
