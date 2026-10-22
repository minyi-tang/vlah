"""Copilot's preToolUse hook against the real tool server: deny on interrupts, pass otherwise."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.tools import make_tools_harness, serving
from tests.fixtures.worker import WORKER, assistant_line
from vlah.arms.config import PRESETS
from vlah.core.types.interrupts import Interrupt

pytestmark = pytest.mark.harness

INTERRUPT = Interrupt(
    id="i-1",
    agent=WORKER,
    kind="hard",
    rule=2,
    reason="your target claim was refuted.",
    written_ts=0.0,
)


def run_hook(
    tool_name: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vlah.adapters.harness.copilot.hook"],
        input=json.dumps({"toolName": tool_name, "toolArgs": {}}),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def hook_environment(
    check_url: str, ack_url: str, token: str, transcript: Path
) -> dict[str, str]:
    return {
        "VLAH_HOOK_CHECK_URL": check_url,
        "VLAH_HOOK_ACK_URL": ack_url,
        "VLAH_TOOL_TOKEN": token,
        "VLAH_TRANSCRIPT": str(transcript),
        "VLAH_CONTEXT_RESTART_FRACTION": "0.7",
        "VLAH_WRAP_UP_TOOLS": "herd-repo_write,herd-surprise",
    }


def denial(completed: subprocess.CompletedProcess[str]) -> str | None:
    assert completed.returncode == 0, "Copilot denies the call when a hook errors"
    if completed.stdout:
        output = json.loads(completed.stdout)
        assert output["permissionDecision"] == "deny"
        return str(output["permissionDecisionReason"])
    else:
        return None


def test_the_hook_denies_on_a_hard_interrupt_and_passes_otherwise(
    tmp_path: Path,
) -> None:
    tools = make_tools_harness(tmp_path, PRESETS["S2"])
    token = tools.tools.register_agent(WORKER, tmp_path)
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(assistant_line(50_000, "bash"))
    with serving(tools.tools) as server:
        hooks = server.hook_config_for(WORKER)
        environment = hook_environment(
            hooks.interrupt_check_url, hooks.interrupt_ack_url, token, transcript
        )
        assert denial(run_hook("bash", environment)) is None
        tools.router.pending[WORKER] = INTERRUPT
        reason = denial(run_hook("view", environment))
        assert reason is not None and INTERRUPT.reason in reason
        assert tools.router.acknowledged == ["i-1"], "the agent has now seen it"
        assert denial(run_hook("view", environment)) is None


def test_past_the_threshold_only_the_wrap_up_tools_pass(tmp_path: Path) -> None:
    tools = make_tools_harness(tmp_path, PRESETS["S2"])
    token = tools.tools.register_agent(WORKER, tmp_path)
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(assistant_line(150_000, "bash"))
    with serving(tools.tools) as server:
        hooks = server.hook_config_for(WORKER)
        environment = hook_environment(
            hooks.interrupt_check_url, hooks.interrupt_ack_url, token, transcript
        )
        reason = denial(run_hook("bash", environment))
        assert reason is not None and "75% full" in reason
        assert denial(run_hook("herd-repo_write", environment)) is None


def test_a_broken_hook_lets_the_call_through(tmp_path: Path) -> None:
    environment = hook_environment(
        "http://127.0.0.1:9/hook/check",
        "http://127.0.0.1:9/hook/ack",
        "token",
        tmp_path / "events.jsonl",
    )
    completed = run_hook("bash", environment)
    assert completed.returncode == 0 and completed.stdout == ""
    assert "letting the call through" in completed.stderr
