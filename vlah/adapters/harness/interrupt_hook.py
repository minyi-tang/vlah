"""The harness-side interrupt check before every tool call (D60 layer 2, D77).

Every harness with tool hooks runs a small script before each tool call, built-in
tools included. The script parses its harness's payload and prints its harness's
decision format; the decision itself is made here:
1. ask the tool server's /hook/check for a pending hard interrupt; if there is one,
   acknowledge it and deny the call with the interrupt text, so the agent reads it;
2. otherwise, once the context passes context_restart_fraction, deny every call
   except the herd tools the agent needs to write its contribution claim (D77);
3. otherwise let the call through, to the harness's normal permission rules.

The interrupt check needs the network and the context check does not, so when the
tool server cannot be reached the context check still decides; the executor stays
the fallback for interrupts (D60 layer 3). A broken hook must not stop the agent.

Every interrupt the hook shows the agent is appended to .vlah/interrupts.jsonl in
the sandbox, so the worker loop can put it in a context-restart pack (D77 part 3).

The protocol, from the environment the harness sets:
- VLAH_HOOK_CHECK_URL, VLAH_HOOK_ACK_URL: the tool server's hook endpoints;
- VLAH_TOOL_TOKEN: the agent's bearer token;
- VLAH_TRANSCRIPT and VLAH_SANDBOX (set by the executor) and
  VLAH_CONTEXT_RESTART_FRACTION;
- VLAH_WRAP_UP_TOOLS: comma-separated tool names that stay allowed past the threshold.
"""

import json
import sys
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vlah.core.worker.restart import INTERRUPTS_FILE

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
    sandbox: Path | None
    restart_fraction: float
    wrap_up_tools: frozenset[str]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "HookSettings":
        transcript = environment.get("VLAH_TRANSCRIPT")
        sandbox = environment.get("VLAH_SANDBOX")
        return cls(
            check_url=environment["VLAH_HOOK_CHECK_URL"],
            ack_url=environment["VLAH_HOOK_ACK_URL"],
            token=environment["VLAH_TOOL_TOKEN"],
            transcript=Path(transcript) if transcript else None,
            sandbox=Path(sandbox) if sandbox else None,
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


def wrap_up_reason(
    tool_name: str, settings: HookSettings, context_fraction: ContextFraction
) -> str | None:
    """The D77 denial past the context threshold; needs no network."""
    if settings.transcript is None or tool_name in settings.wrap_up_tools:
        return None
    else:
        fraction = context_fraction(settings.transcript)
        if fraction >= settings.restart_fraction:
            return WRAP_UP_MESSAGE.format(percent=round(fraction * 100))
        else:
            return None


def record_interrupt(settings: HookSettings, interrupt: dict[object, object]) -> None:
    """Keep what the agent was shown, for the restart pack; never fail the hook over it."""
    if settings.sandbox is None:
        return
    else:
        path = settings.sandbox / INTERRUPTS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as lines:
                lines.write(json.dumps(interrupt, default=str) + "\n")
        except OSError as error:
            print(f"vlah hook could not record the interrupt: {error}", file=sys.stderr)


def pending_interrupt(settings: HookSettings) -> dict[object, object] | None:
    """Ask the tool server; acknowledge the interrupt, since the agent is about to read it."""
    reply = post_json(settings.check_url, settings.token, {})
    interrupt = reply.get("interrupt")
    if isinstance(interrupt, dict):
        post_json(
            settings.ack_url, settings.token, {"interrupt_id": str(interrupt["id"])}
        )
        return interrupt
    else:
        return None


def denial_reason(
    tool_name: str, settings: HookSettings, context_fraction: ContextFraction
) -> str | None:
    """Why to deny this call, or None to let it through.

    An interrupt comes first. When the tool server cannot be reached, the context
    check decides on its own, so the wrap-up guard never fails open.
    """
    wrap_up = wrap_up_reason(tool_name, settings, context_fraction)
    try:
        interrupt = pending_interrupt(settings)
    except Exception as error:
        print(f"vlah hook could not check interrupts: {error}", file=sys.stderr)
        return wrap_up
    if interrupt is not None:
        record_interrupt(settings, interrupt)
        return interrupt_text(interrupt)
    else:
        return wrap_up
