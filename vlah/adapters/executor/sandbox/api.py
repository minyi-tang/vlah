"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

The sandbox egress allowlist: workers reach only the model API, the herd tool
server, declared context sources and package mirrors (v2.2 D75).
Implemented by W1-12 (#22).
"""

from dataclasses import dataclass
from typing import Protocol

from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy


@dataclass(frozen=True, kw_only=True)
class HerdHosts:
    """Destinations every sandbox may reach, whatever the charter says."""

    tool_server_host: str
    tool_server_port: int
    slow_eval_host: str | None
    model_api_host: str


@dataclass(frozen=True, kw_only=True)
class WrappedCommand:
    argv: tuple[str, ...]
    env: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class DeniedConnection:
    timestamp_seconds: float
    agent: ActorId
    host: str
    port: int


class BuildEffectivePolicy(Protocol):
    def __call__(
        self, charter_policy: EgressPolicy, herd_hosts: HerdHosts
    ) -> EgressPolicy:
        """The charter's allowlist plus the herd's own hosts; never reads files."""
        ...


class Sandbox(Protocol):
    def wrap(
        self,
        agent: ActorId,
        argv: tuple[str, ...],
        env: dict[str, str],
        policy: EgressPolicy,
    ) -> WrappedCommand:
        """Raise RuntimeError if the platform cannot enforce the policy below the proxy."""
        ...

    def denied(self) -> tuple[DeniedConnection, ...]: ...
