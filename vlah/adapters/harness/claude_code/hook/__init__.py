"""The PreToolUse hook: the interrupt check before every tool call (D60 layer 2, D77).

Claude Code runs "python -m vlah.adapters.harness.claude_code.hook" before each tool
call, built-in tools included, with the call as JSON on stdin. The hook:
1. asks the tool server's /hook/check for a pending hard interrupt; if there is one,
   it acknowledges it and denies the call with the interrupt text, so the agent
   reads it next;
2. otherwise, once the context passes context_restart_fraction, denies every call
   except the herd tools the agent needs to write its contribution claim (D77);
3. otherwise says nothing, and Claude Code's normal permission rules apply.

Any failure exits 1, which Claude Code treats as "proceed": a broken hook must not
stop the agent, and the executor remains the fallback for interrupts (D60 layer 3).

The protocol, from the environment the harness sets:
- VLAH_HOOK_CHECK_URL, VLAH_HOOK_ACK_URL: the tool server's hook endpoints;
- VLAH_TOOL_TOKEN: the agent's bearer token;
- VLAH_TRANSCRIPT, VLAH_CONTEXT_WINDOW_TOKENS, VLAH_CONTEXT_RESTART_FRACTION.
"""

import json
import sys
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from vlah.adapters.harness.claude_code.transcript import context_tokens

HOOK_TIMEOUT_SECONDS = 2.0
HERD_SERVER_NAME = "herd"
# What an agent over the context threshold may still call: enough to look up and
# write its contribution claim.
WRAP_UP_TOOLS = frozenset(
    f"mcp__{HERD_SERVER_NAME}__{name}"
    for name in ("repo_write", "surprise", "repo_read", "search")
)
WRAP_UP_MESSAGE = (
    "Your context is {percent}% full. Before anything else, write a claim with "
    "repo_write that records what you found, your unfinished leads and your "
    "surprises; then end the session. The herd starts a fresh session that "
    "continues this task from your claims."
)


@dataclass(frozen=True, kw_only=True)
class HookSettings:
    check_url: str
    ack_url: str
    token: str
    transcript: Path | None
    window_tokens: int
    restart_fraction: float

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "HookSettings":
        transcript = environment.get("VLAH_TRANSCRIPT")
        return cls(
            check_url=environment["VLAH_HOOK_CHECK_URL"],
            ack_url=environment["VLAH_HOOK_ACK_URL"],
            token=environment["VLAH_TOOL_TOKEN"],
            transcript=Path(transcript) if transcript else None,
            window_tokens=int(environment["VLAH_CONTEXT_WINDOW_TOKENS"]),
            restart_fraction=float(environment["VLAH_CONTEXT_RESTART_FRACTION"]),
        )


def post_json(url: str, token: str, body: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HOOK_TIMEOUT_SECONDS) as response:
        reply: object = json.loads(response.read())
    if isinstance(reply, dict):
        return {str(key): value for key, value in reply.items()}
    else:
        raise ValueError(f"{url} answered {reply!r}")


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


def interrupt_text(interrupt: dict[object, object]) -> str:
    return (
        f"The herd interrupted you ({interrupt.get('kind')} interrupt, rule "
        f"{interrupt.get('rule')}): {interrupt.get('reason')} Act on this before "
        "anything else."
    )


def decide(tool_name: str, settings: HookSettings) -> str | None:
    """The JSON to print, or None to let the call through."""
    reply = post_json(settings.check_url, settings.token, {})
    interrupt = reply.get("interrupt")
    if isinstance(interrupt, dict):
        post_json(
            settings.ack_url, settings.token, {"interrupt_id": str(interrupt["id"])}
        )
        return deny(interrupt_text(interrupt))
    elif settings.transcript is None or tool_name in WRAP_UP_TOOLS:
        return None
    else:
        fraction = context_tokens(settings.transcript) / settings.window_tokens
        if fraction >= settings.restart_fraction:
            return deny(WRAP_UP_MESSAGE.format(percent=round(fraction * 100)))
        else:
            return None


def main(stdin: TextIO, stdout: TextIO, environment: Mapping[str, str]) -> int:
    try:
        payload: object = json.load(stdin)
        tool_name = payload.get("tool_name") if isinstance(payload, dict) else None
        decision = decide(str(tool_name), HookSettings.from_environment(environment))
    except Exception as error:
        print(f"vlah hook failed, letting the call through: {error}", file=sys.stderr)
        return 1
    if decision is not None:
        stdout.write(decision)
    else:
        pass
    return 0
