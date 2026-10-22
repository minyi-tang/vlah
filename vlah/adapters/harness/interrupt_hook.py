"""The harness-side interrupt check before every tool call (D60 layer 2, D77).

Every harness with tool hooks runs a small script before each tool call, built-in
tools included. The script parses its harness's payload and prints its harness's
decision format; the decision itself is made here:
1. ask the tool server's /hook/check for a pending hard interrupt; if there is one,
   acknowledge it and deny the call with the interrupt text, so the agent reads it;
2. otherwise, once the context passes context_restart_fraction, deny every call
   except the herd tools the agent needs to write its contribution claim (D77);
3. otherwise let the call through, to the harness's normal permission rules.

The script must let the call through when anything here fails: a broken hook must
not stop the agent, and the executor stays the fallback (D60 layer 3).

The protocol, from the environment the harness sets:
- VLAH_HOOK_CHECK_URL, VLAH_HOOK_ACK_URL: the tool server's hook endpoints;
- VLAH_TOOL_TOKEN: the agent's bearer token;
- VLAH_TRANSCRIPT (set by the executor) and VLAH_CONTEXT_RESTART_FRACTION;
- VLAH_WRAP_UP_TOOLS: comma-separated tool names that stay allowed past the threshold.
"""

import json
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

HOOK_TIMEOUT_SECONDS = 2.0
HERD_SERVER_NAME = "herd"
WRAP_UP_TOOL_NAMES = ("repo_write", "surprise", "repo_read", "search")
WRAP_UP_MESSAGE = (
    "Your context is {percent}% full. Before anything else, write a claim with "
    "repo_write that records what you found, your unfinished leads and your "
    "surprises; then end the session. The herd starts a fresh session that "
    "continues this task from your claims."
)


class ContextFraction(Protocol):
    def __call__(self, transcript_path: Path) -> float:
        """How full the context window is, from the harness's transcript."""
        ...


@dataclass(frozen=True, kw_only=True)
class HookSettings:
    check_url: str
    ack_url: str
    token: str
    transcript: Path | None
    restart_fraction: float
    wrap_up_tools: frozenset[str]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "HookSettings":
        transcript = environment.get("VLAH_TRANSCRIPT")
        return cls(
            check_url=environment["VLAH_HOOK_CHECK_URL"],
            ack_url=environment["VLAH_HOOK_ACK_URL"],
            token=environment["VLAH_TOOL_TOKEN"],
            transcript=Path(transcript) if transcript else None,
            restart_fraction=float(environment["VLAH_CONTEXT_RESTART_FRACTION"]),
            wrap_up_tools=frozenset(
                name
                for name in environment.get("VLAH_WRAP_UP_TOOLS", "").split(",")
                if name
            ),
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


def interrupt_text(interrupt: dict[object, object]) -> str:
    return (
        f"The herd interrupted you ({interrupt.get('kind')} interrupt, rule "
        f"{interrupt.get('rule')}): {interrupt.get('reason')} Act on this before "
        "anything else."
    )


def denial_reason(
    tool_name: str, settings: HookSettings, context_fraction: ContextFraction
) -> str | None:
    """Why to deny this call, or None to let it through."""
    reply = post_json(settings.check_url, settings.token, {})
    interrupt = reply.get("interrupt")
    if isinstance(interrupt, dict):
        post_json(
            settings.ack_url, settings.token, {"interrupt_id": str(interrupt["id"])}
        )
        return interrupt_text(interrupt)
    elif settings.transcript is None or tool_name in settings.wrap_up_tools:
        return None
    else:
        fraction = context_fraction(settings.transcript)
        if fraction >= settings.restart_fraction:
            return WRAP_UP_MESSAGE.format(percent=round(fraction * 100))
        else:
            return None
