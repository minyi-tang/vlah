"""The prompt conformance cases on a real model (slow, manual, costs model calls).

Run it on purpose, before a prompt change merges:

    uv run pytest -m slow tests/unit/harness/test_prompt_conformance_live.py -s

- VLAH_CONFORMANCE_HARNESS: copilot (the default when its CLI is installed) or
  claude_code.
- VLAH_E2E_MODEL: the model; Copilot falls back to its own choice, Claude Code needs it.
- VLAH_CONFORMANCE_REPEATS: sessions per case (default 1). Behaviour varies between
  sessions, so use 3 before a prompt change merges; a boundary must hold in all.
- VLAH_CONFORMANCE_REPORT: a file every case's table is appended to.

A case fails only on a boundary. Format and suggestion findings are in the table.
"""

import os
import shutil
from pathlib import Path

import pytest

from tests.fixtures.conformance import CASES, Case, run_case
from vlah.adapters.harness.claude_code.harness import (
    ClaudeCodeConfig,
    ClaudeCodeHarness,
)
from vlah.adapters.harness.copilot.harness import CopilotConfig, CopilotHarness
from vlah.core.types.params import Params
from vlah.ports.harness import Harness
from vlah.testing.prompt_conformance import (
    TranscriptReader,
    boundary_failures,
    claude_code_tool_calls,
    copilot_tool_calls,
    render_report,
)

COPILOT = "copilot"
CLAUDE_CODE = "claude_code"
HARNESS_KIND = os.environ.get("VLAH_CONFORMANCE_HARNESS") or (
    COPILOT if shutil.which("copilot") is not None else CLAUDE_CODE
)
MODEL = os.environ.get("VLAH_E2E_MODEL")
REPEATS = int(os.environ.get("VLAH_CONFORMANCE_REPEATS", "1"))
DEFAULT_WINDOW_TOKENS = 200_000
COMMANDS = {COPILOT: "copilot", CLAUDE_CODE: "claude"}
READERS: dict[str, TranscriptReader] = {
    COPILOT: copilot_tool_calls,
    CLAUDE_CODE: claude_code_tool_calls,
}

pytestmark = [
    pytest.mark.harness,
    pytest.mark.slow,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        shutil.which(COMMANDS.get(HARNESS_KIND, HARNESS_KIND)) is None,
        reason=f"needs the {HARNESS_KIND} CLI",
    ),
    pytest.mark.skipif(
        HARNESS_KIND == CLAUDE_CODE and MODEL is None,
        reason="Claude Code needs VLAH_E2E_MODEL",
    ),
]


def make_harness(params: Params) -> Harness:
    if HARNESS_KIND == COPILOT:
        return CopilotHarness(
            config=CopilotConfig(model=MODEL or "auto"), params=params
        )
    else:
        assert MODEL is not None
        return ClaudeCodeHarness(
            config=ClaudeCodeConfig(
                model=MODEL,
                context_window_tokens=int(
                    os.environ.get("VLAH_E2E_WINDOW_TOKENS", DEFAULT_WINDOW_TOKENS)
                ),
            ),
            params=params,
        )


@pytest.mark.parametrize("repeat", range(REPEATS))
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_real_session_breaks_no_boundary_of_the_prompt(
    case: Case, repeat: int, tmp_path: Path
) -> None:
    result = run_case(
        case, tmp_path, make_harness, READERS[HARNESS_KIND], dict(os.environ)
    )
    report = render_report(
        f"{case.name}, session {repeat + 1}, prompt {result.prompt_hash}, "
        f"{HARNESS_KIND} {MODEL or 'auto'}",
        result.findings,
    )
    print(report)
    report_path = os.environ.get("VLAH_CONFORMANCE_REPORT")
    if report_path is not None:
        with Path(report_path).open("a", encoding="utf-8") as reports:
            reports.write(report + "\n")
    else:
        pass
    assert not boundary_failures(result.findings), report
