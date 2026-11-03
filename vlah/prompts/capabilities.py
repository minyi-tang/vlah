"""Capabilities: each is a bundle of tools, the hosts they need and one prompt card.

Granting a capability to a role adds its card to the prompt and its tools, MCP
servers and hosts to the harness in one step, so a new tool set (a search server,
computer use) is a bundle here and never a prompt rewrite (docs/role-prompts.md).
"""

from dataclasses import dataclass, field
from importlib.resources import files
from typing import Literal

from vlah.adapters.harness.interrupt_hook import HERD_SERVER_NAME

HarnessKind = Literal["claude_code", "copilot"]
CARDS = files("vlah.prompts").joinpath("cards")


@dataclass(frozen=True, kw_only=True)
class McpServer:
    """A static MCP server a capability brings; the herd's own is configured per session."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, kw_only=True)
class Capability:
    name: str
    # Tool patterns per harness; Copilot allows every tool, so it lists none.
    tools: dict[HarnessKind, tuple[str, ...]]
    mcp_servers: tuple[McpServer, ...] = ()
    # Hosts the sandbox may reach for this capability (D75).
    allowed_urls: tuple[str, ...] = ()
    # One line for the boundaries section.
    boundary: str
    card: str


def card(name: str) -> str:
    return CARDS.joinpath(f"{name}.md").read_text(encoding="utf-8")


HERD = Capability(
    name=HERD_SERVER_NAME,
    tools={"claude_code": (f"mcp__{HERD_SERVER_NAME}__*",)},
    boundary="The herd tools are the only way to the blackboard and the judge.",
    card=card("herd"),
)
SHELL = Capability(
    name="shell",
    tools={"claude_code": ("Bash", "Read", "Edit", "Write", "Glob", "Grep")},
    boundary="The shell reaches only the hosts on the egress allowlist.",
    card=card("shell"),
)
CAPABILITIES = {capability.name: capability for capability in (HERD, SHELL)}
