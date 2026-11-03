"""A role profile: the role's prompt files and the capabilities it is granted.

Roles differ in three things only: the mission files, the capability set and what
the store lets them see (docs/role-prompts.md). The worker is the only role that
runs in the alpha; a run's herd.yaml may change its capability set.
"""

from dataclasses import dataclass, replace

from vlah.prompts.capabilities import (
    CAPABILITIES,
    HERD,
    SHELL,
    Capability,
    HarnessKind,
    McpServer,
)


@dataclass(frozen=True, kw_only=True)
class RoleProfile:
    role: str
    # The package holding mission.md, workspace.md and suggestions.md.
    prompt_package: str
    capabilities: tuple[Capability, ...]

    def has(self, capability_name: str) -> bool:
        return any(
            capability.name == capability_name for capability in self.capabilities
        )

    def tools(self, harness: HarnessKind) -> tuple[str, ...]:
        return tuple(
            pattern
            for capability in self.capabilities
            for pattern in capability.tools.get(harness, ())
        )

    def mcp_servers(self) -> tuple[McpServer, ...]:
        return tuple(
            server
            for capability in self.capabilities
            for server in capability.mcp_servers
        )

    def allowed_urls(self) -> tuple[str, ...]:
        return tuple(
            url for capability in self.capabilities for url in capability.allowed_urls
        )


WORKER = RoleProfile(
    role="worker", prompt_package="vlah.prompts.worker", capabilities=(HERD, SHELL)
)


def with_capabilities(profile: RoleProfile, names: tuple[str, ...]) -> RoleProfile:
    """The profile with the named capabilities instead, as herd.yaml sets them."""
    unknown = [name for name in names if name not in CAPABILITIES]
    if unknown:
        raise ValueError(
            f"herd.yaml: unknown capabilities {unknown}; known: {sorted(CAPABILITIES)}"
        )
    else:
        return replace(
            profile, capabilities=tuple(CAPABILITIES[name] for name in names)
        )
