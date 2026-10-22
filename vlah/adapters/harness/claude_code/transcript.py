"""Reading Claude Code's stream-json output, which the runner copies to VLAH_TRANSCRIPT.

The docs call the session files under ~/.claude internal and liable to change, and
recommend stream-json for scripts. Both carry assistant messages in the same shape:
{"type": "assistant", "message": {"content": [...], "usage": {...}}}.

Context use is what the next request sends: input_tokens plus both cache counts
on the latest assistant message. Output tokens are left out, as the status line
does, because per-step output counts are placeholders.
"""

import json
from collections.abc import Iterator
from pathlib import Path

INPUT_USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
TOOL_CALL_TEXT_LIMIT = 300
RATE_LIMIT_STATUS = 429


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


def assistant_messages(transcript_path: Path) -> Iterator[dict[str, object]]:
    for record in records(transcript_path):
        message = record.get("message")
        if record.get("type") == "assistant" and isinstance(message, dict):
            yield {str(key): value for key, value in message.items()}
        else:
            continue


def count_tokens(usage: object) -> int:
    if not isinstance(usage, dict):
        return 0
    else:
        return sum(
            value
            for field in INPUT_USAGE_FIELDS
            if isinstance(value := usage.get(field), int)
        )


def context_tokens(transcript_path: Path) -> int:
    """Tokens in the context as of the latest assistant message; 0 before the first."""
    latest = 0
    for message in assistant_messages(transcript_path):
        if "usage" in message:
            latest = count_tokens(message["usage"])
        else:
            pass
    return latest


def describe_tool_call(block: dict[object, object]) -> str:
    text = f"{block.get('name')} {json.dumps(block.get('input'), sort_keys=True)}"
    if len(text) > TOOL_CALL_TEXT_LIMIT:
        return text[: TOOL_CALL_TEXT_LIMIT - 3] + "..."
    else:
        return text


def tool_calls(transcript_path: Path) -> list[str]:
    calls: list[str] = []
    for message in assistant_messages(transcript_path):
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append(describe_tool_call(block))
            else:
                continue
    return calls


def rate_limit_retries(transcript_path: Path) -> int:
    """How many times Claude Code retried after a 429, from its api_retry events."""
    return sum(
        1
        for record in records(transcript_path)
        if record.get("type") == "system"
        and record.get("subtype") == "api_retry"
        and record.get("error_status") == RATE_LIMIT_STATUS
    )
