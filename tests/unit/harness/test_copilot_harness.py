"""What the Copilot harness asks Copilot CLI to do, its runner, and how it reads usage."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.worker import (
    TASK,
    WINDOW_TOKENS,
    WORKER,
    assistant_line,
    rate_limit_line,
)
from vlah.adapters.harness.copilot.harness import (
    MCP_CONFIG_PATH,
    RUNNER_MODULE,
    SETTINGS_PATH,
    CopilotConfig,
    CopilotHarness,
)
from vlah.adapters.harness.copilot.runner import session_id_for
from vlah.core.types.params import Params
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import Handle, StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.testing.conformance import HarnessCheckInputs, check_harness

pytestmark = pytest.mark.harness

CONFIG = CopilotConfig(
    model="model-from-herd-yaml",
    reasoning_effort="max",
    python_executable="/venv/bin/python",
    allowed_urls=("http://127.0.0.1:8200",),
    environment={"SOME_PASSTHROUGH": "yes"},
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


def harness(config: CopilotConfig = CONFIG) -> CopilotHarness:
    return CopilotHarness(config=config, params=Params())


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


def test_build_runs_copilot_with_the_herd_server_and_the_hook() -> None:
    spec = harness().build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    command = list(spec.command)
    assert command[:3] == ["/venv/bin/python", "-m", RUNNER_MODULE]
    copilot = command[3:]
    assert copilot[:2] == ["copilot", "-p"]
    assert "Make the solver faster." in copilot[2], "the prompt states the task's goal"

    def flag(name: str) -> str:
        return copilot[copilot.index(name) + 1]

    assert flag("--output-format") == "json"
    for switch in (
        "--allow-all-tools",
        "--no-ask-user",
        "--no-auto-update",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
    ):
        assert switch in copilot
    assert "--allow-all-paths" not in copilot, "file access stays in the sandbox"
    assert flag("--additional-mcp-config") == f"@{MCP_CONFIG_PATH}"
    assert flag("--model") == "model-from-herd-yaml"
    assert flag("--reasoning-effort") == "max"
    assert flag("--allow-url") == "http://127.0.0.1:8200"

    server = json.loads(spec.files[MCP_CONFIG_PATH])["mcpServers"]["herd"]
    assert server == {
        "type": "http",
        "url": ENDPOINT.url,
        "headers": {"Authorization": "Bearer agent-token"},
        "tools": ["*"],
    }
    (hook,) = json.loads(spec.files[SETTINGS_PATH])["hooks"]["preToolUse"]
    assert hook == {
        "type": "command",
        "bash": "/venv/bin/python -m vlah.adapters.harness.copilot.hook",
        "timeoutSec": 10,
    }
    assert spec.env["VLAH_TOOL_TOKEN"] == "agent-token"
    assert spec.env["VLAH_HOOK_CHECK_URL"] == HOOKS.interrupt_check_url
    assert spec.env["VLAH_CONTEXT_RESTART_FRACTION"] == "0.7"
    assert "herd-repo_write" in spec.env["VLAH_WRAP_UP_TOOLS"].split(",")
    assert spec.env["SOME_PASSTHROUGH"] == "yes"
    assert spec.files["CONTEXT.md"] == START_PACK.files["CONTEXT.md"]


def test_usage_comes_from_the_latest_usage_record(tmp_path: Path) -> None:
    transcript = tmp_path / "events.jsonl"
    assert harness().usage(transcript).fraction == 0.0, "nothing used before a call"
    transcript.write_text(
        assistant_line(40_000, "bash")
        + rate_limit_line()
        + "{cut off mid-wri"
        + "\n"
        + assistant_line(150_000, "herd-repo_write")
    )
    usage = harness().usage(transcript)
    assert (usage.used_tokens, usage.window_tokens) == (150_000, WINDOW_TOKENS)
    assert harness().recent_tool_calls(transcript, 1) == (
        'herd-repo_write {"command": "ls"}',
    )
    assert len(harness().recent_tool_calls(transcript, 20)) == 2
    assert harness().rate_limit_retries(transcript) == 1


def test_a_configured_window_covers_records_without_one(tmp_path: Path) -> None:
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "session.usage_record", "data": {"usage": {"inputTokens": 50}}}
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="context_window_tokens"):
        harness().usage(transcript)
    configured = CopilotConfig(model="m", context_window_tokens=100)
    assert harness(configured).usage(transcript).fraction == 0.5


STAND_IN_FOR_COPILOT = """
import os, pathlib, sys
session = sys.argv[sys.argv.index("--session-id") + 1]
events = pathlib.Path(os.environ["COPILOT_HOME"]) / "session-state" / session / "events.jsonl"
events.parent.mkdir(parents=True)
events.write_text('{"type": "session.start"}\\n')
print("streamed to stdout")
sys.exit(3)
"""


def test_the_runner_gives_copilot_a_private_home_and_mirrors_its_events(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / ".vlah" / "local-00001-1.transcript.jsonl"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            RUNNER_MODULE,
            sys.executable,
            "-c",
            STAND_IN_FOR_COPILOT,
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "VLAH_TRANSCRIPT": str(transcript)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 3, completed.stderr
    assert completed.stdout == "streamed to stdout\n"
    assert transcript.read_text() == '{"type": "session.start"}\n'
    events = (
        tmp_path
        / ".vlah"
        / "copilot"
        / "session-state"
        / session_id_for(transcript)
        / "events.jsonl"
    )
    assert events.is_file(), "COPILOT_HOME is the sandbox's .vlah/copilot"
