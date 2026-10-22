"""Copilot's transcript after a compaction: the context is lossy, so it counts as full (D77)."""

import json
from pathlib import Path

import pytest

from tests.fixtures.worker import assistant_line
from vlah.adapters.harness.copilot.harness import CopilotConfig, CopilotHarness
from vlah.adapters.harness.copilot.transcript import context_fraction, latest_context
from vlah.core.types.params import Params

pytestmark = pytest.mark.harness

COMPACTION = json.dumps({"type": "session.compaction_start", "data": {}}) + "\n"


def test_usage_after_a_compaction_reports_the_window_as_full(tmp_path: Path) -> None:
    transcript = tmp_path / "events.jsonl"
    transcript.write_text(assistant_line(165_000, "Bash"))
    harness = CopilotHarness(config=CopilotConfig(model="m"), params=Params())
    assert harness.usage(transcript).fraction == pytest.approx(0.825)
    assert not latest_context(transcript).compacted

    with transcript.open("a") as lines:
        lines.write(COMPACTION + assistant_line(40_000, "Bash"))
    assert latest_context(transcript).compacted
    assert harness.usage(transcript).fraction == 1.0
    assert context_fraction(transcript, None) == 1.0
