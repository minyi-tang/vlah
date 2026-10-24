"""What a worker session may reach, in the terms every harness shares (D75).

A harness lists the herd's own endpoints (the MCP tool server and the interrupt
hook's two URLs), the hosts its configuration allows the agent's shell, and the
hosts the agent CLI itself needs for sign-in and the model API.
"""

from collections.abc import Sequence
from typing import Literal
from urllib.parse import urlsplit

from vlah.core.types.params import EgressPolicy, HostRule
from vlah.ports.harness import HookConfig, ToolEndpoint

HostPurpose = Literal["model_api", "tools", "context", "mirror", "testbed"]
DEFAULT_HTTPS_PORT = 443
DEFAULT_HTTP_PORT = 80


def host_and_port(url: str) -> tuple[str, int]:
    """The destination of a URL, or of a bare domain as --allow-url accepts it."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    if parts.hostname is None:
        raise ValueError(f"{url!r} names no host")
    elif parts.port is not None:
        return parts.hostname, parts.port
    else:
        return parts.hostname, (
            DEFAULT_HTTP_PORT if parts.scheme == "http" else DEFAULT_HTTPS_PORT
        )


def url_rules(urls: Sequence[str], purpose: HostPurpose) -> list[HostRule]:
    rules: list[HostRule] = []
    for url in urls:
        host, port = host_and_port(url)
        rules.append(HostRule(host=host, ports=(port,), purpose=purpose))
    return rules


def herd_tool_rules(
    tool_endpoint: ToolEndpoint, hook_config: HookConfig
) -> list[HostRule]:
    return url_rules(
        (
            tool_endpoint.url,
            hook_config.interrupt_check_url,
            hook_config.interrupt_ack_url,
        ),
        "tools",
    )


def session_policy(
    tool_endpoint: ToolEndpoint,
    hook_config: HookConfig,
    allowed_urls: Sequence[str],
    model_api_rules: Sequence[HostRule],
) -> EgressPolicy:
    """The herd's tools, the allowed URLs, then the model API, without repeats."""
    rules = [
        *herd_tool_rules(tool_endpoint, hook_config),
        *url_rules(allowed_urls, "testbed"),
        *model_api_rules,
    ]
    return EgressPolicy(allow=tuple(dict.fromkeys(rules)))
