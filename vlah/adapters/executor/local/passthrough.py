"""A Sandbox that enforces nothing, until the egress allowlist lands (W1-12, #22).

Use it only in tests and local development: with it, a worker can reach any host.
"""

from vlah.adapters.executor.sandbox.api import DeniedConnection, WrappedCommand
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy


class PassthroughSandbox:
    def wrap(
        self,
        agent: ActorId,
        argv: tuple[str, ...],
        env: dict[str, str],
        policy: EgressPolicy,
    ) -> WrappedCommand:
        return WrappedCommand(argv=argv, env=dict(env))

    def denied(self) -> tuple[DeniedConnection, ...]:
        return ()
