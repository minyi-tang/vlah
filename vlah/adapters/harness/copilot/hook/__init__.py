"""Copilot CLI's preToolUse hook: the herd's interrupt check in Copilot's format.

Copilot runs "python -m vlah.adapters.harness.copilot.hook" before every tool call
with {"toolName": ..., "toolArgs": ...} on stdin. To deny, the hook prints
{"permissionDecision": "deny", "permissionDecisionReason": ...}; to let the call
through, it prints nothing. Copilot denies the call when a hook errors, so this
hook always exits 0 and lets the call through when anything fails.
"""

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from vlah.adapters.harness.copilot.transcript import context_fraction
from vlah.adapters.harness.interrupt_hook import HookSettings, denial_reason


@dataclass(frozen=True, kw_only=True)
class CopilotContextFraction:
    fallback_window_tokens: int | None

    def __call__(self, transcript_path: Path) -> float:
        return context_fraction(transcript_path, self.fallback_window_tokens)


def deny(reason: str) -> str:
    return json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": reason}
    )


def main(stdin: TextIO, stdout: TextIO, environment: Mapping[str, str]) -> int:
    try:
        payload: object = json.load(stdin)
        tool_name = payload.get("toolName") if isinstance(payload, dict) else None
        window = environment.get("VLAH_CONTEXT_WINDOW_TOKENS")
        reason = denial_reason(
            str(tool_name),
            HookSettings.from_environment(environment),
            CopilotContextFraction(
                fallback_window_tokens=int(window) if window else None
            ),
        )
    except Exception as error:
        print(f"vlah hook failed, letting the call through: {error}", file=sys.stderr)
        return 0
    if reason is not None:
        stdout.write(deny(reason))
    else:
        pass
    return 0
