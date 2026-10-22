"""Reading Copilot CLI's session events, which the runner copies to VLAH_TRANSCRIPT.

Copilot persists every session's events to $COPILOT_HOME/session-state/<id>/events.jsonl.
Its stdout stream (--output-format json) carries no token counts, so the runner
mirrors the persisted file instead. The events used here:
- session.usage_record: one per model call, with usage.inputTokens (cache reads and
  writes included) and usage.maxPromptTokens, the model's context window;
- tool.execution_start: toolName and arguments of each tool call;
- session.error with errorType "rate_limit", and assistant.turn_retry: throttling;
- session.compaction_start: Copilot compacted the conversation itself.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

TOOL_CALL_TEXT_LIMIT = 300


@dataclass(frozen=True, kw_only=True)
class ContextReading:
    used_tokens: int
    window_tokens: int | None


def records(transcript_path: Path) -> Iterator[dict[str, object]]:
    """Every JSON object in the file; a line cut off mid-write is skipped."""
    if not transcript_path.is_file():
        return
    else:
        with transcript_path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    record: object = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield {str(key): value for key, value in record.items()}
                else:
                    continue


def event_data(record: dict[str, object]) -> dict[object, object]:
    data = record.get("data")
    return data if isinstance(data, dict) else {}


def latest_context(transcript_path: Path) -> ContextReading:
    """Context use as of the latest model call; zero before the first."""
    reading = ContextReading(used_tokens=0, window_tokens=None)
    for record in records(transcript_path):
        usage = event_data(record).get("usage")
        if record.get("type") == "session.usage_record" and isinstance(usage, dict):
            used = usage.get("inputTokens")
            window = usage.get("maxPromptTokens")
            reading = ContextReading(
                used_tokens=used if isinstance(used, int) else 0,
                window_tokens=(
                    window if isinstance(window, int) and window > 0 else None
                ),
            )
        else:
            continue
    return reading


def context_fraction(
    transcript_path: Path, fallback_window_tokens: int | None
) -> float:
    reading = latest_context(transcript_path)
    window = reading.window_tokens or fallback_window_tokens
    if window is None or window <= 0:
        return 0.0
    else:
        return reading.used_tokens / window


def describe_tool_call(data: dict[object, object]) -> str:
    text = f"{data.get('toolName')} {json.dumps(data.get('arguments'), sort_keys=True)}"
    if len(text) > TOOL_CALL_TEXT_LIMIT:
        return text[: TOOL_CALL_TEXT_LIMIT - 3] + "..."
    else:
        return text


def tool_calls(transcript_path: Path) -> list[str]:
    return [
        describe_tool_call(event_data(record))
        for record in records(transcript_path)
        if record.get("type") == "tool.execution_start"
    ]


def is_rate_limit(record: dict[str, object]) -> bool:
    data = event_data(record)
    if record.get("type") == "session.error":
        return data.get("errorType") == "rate_limit"
    elif record.get("type") == "assistant.turn_retry":
        return "rate" in str(data.get("reason", "")).lower()
    else:
        return False


def rate_limit_events(transcript_path: Path) -> int:
    """Throttling errors and retries in the session, a diagnostic (C7)."""
    return sum(1 for record in records(transcript_path) if is_rate_limit(record))


def compactions(transcript_path: Path) -> int:
    return sum(
        1
        for record in records(transcript_path)
        if record.get("type") == "session.compaction_start"
    )
