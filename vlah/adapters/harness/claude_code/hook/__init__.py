"""Claude Code's PreToolUse hook: the herd's interrupt check in Claude Code's format.

Claude Code runs "python -m vlah.adapters.harness.claude_code.hook" before every
tool call, built-in tools included, with {"tool_name": ..., "tool_input": ...} on
stdin. To deny, the hook prints {"hookSpecificOutput": {"hookEventName":
"PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": ...}}; to
let the call through, it prints nothing. The decision is the shared one in
vlah.adapters.harness.interrupt_hook. A broken hook must not stop the agent, so
any failure prints nothing and exits 0.
"""

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from vlah.adapters.harness.claude_code.transcript import context_tokens
from vlah.adapters.harness.interrupt_hook import HookSettings, denial_reason


@dataclass(frozen=True, kw_only=True)
class ClaudeContextFraction:
    window_tokens: int

    def __call__(self, transcript_path: Path) -> float:
        if self.window_tokens <= 0:
            return 0.0
        else:
            return context_tokens(transcript_path) / self.window_tokens


def deny(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def main(stdin: TextIO, stdout: TextIO, environment: Mapping[str, str]) -> int:
    try:
        payload: object = json.load(stdin)
        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        reason = denial_reason(
            str(tool_name),
            HookSettings.from_environment(environment),
            ClaudeContextFraction(
                window_tokens=int(environment["VLAH_CONTEXT_WINDOW_TOKENS"])
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
