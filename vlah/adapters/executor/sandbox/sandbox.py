"""The sandbox: one proxy per agent (layer 1) inside an OS boundary (layer 2) (v2.2 D75).

wrap starts an allowlist proxy for the agent and returns the command and
environment that run the agent inside the platform's layer 2:
- macOS: a sandbox-exec profile that allows only the proxy port and the herd's own
  local ports, and denies writes under the protected directories;
- Linux: unprivileged user, network and mount namespaces, with a bridge to the
  proxy and the protected directories mounted read-only.
Without layer 2 a process that ignores proxy variables could still get out, so
wrap refuses to start unless allow_unenforced=True, which is for development only
and logs a loud warning on every wrap.

Every denied connection is kept, exposed through denied(), and appended to
<log directory>/<agent>-denied.tsv, which the executor can archive with the trace.
"""

import logging
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.executor.sandbox import linux_namespace, macos
from vlah.adapters.executor.sandbox.api import DeniedConnection, WrappedCommand
from vlah.adapters.executor.sandbox.policy import is_local
from vlah.adapters.executor.sandbox.proxy import (
    AllowlistProxy,
    proxy_environment,
    unix_socket_path_for,
)
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy

logger = logging.getLogger(__name__)

LAYER_MACOS = "macos-sandbox-exec"
LAYER_LINUX = "linux-namespaces"
LAYER_NONE = "none"
LOCAL_NO_PROXY = "localhost,127.0.0.1"


def detect_layer_two() -> str:
    system = platform.system()
    if system == "Darwin" and macos.sandbox_exec_path() is not None:
        return LAYER_MACOS
    elif system == "Linux" and linux_namespace.namespaces_available():
        return LAYER_LINUX
    else:
        return LAYER_NONE


@dataclass(kw_only=True)
class ProcessSandbox:
    """Sandbox for the local executor; one instance per run."""

    clock: Clock
    work_directory: Path
    protected_directories: tuple[Path, ...] = ()
    allow_unenforced: bool = False
    layer_two: str = field(default_factory=detect_layer_two)
    python_executable: str = field(default_factory=lambda: sys.executable)
    proxies: dict[ActorId, AllowlistProxy] = field(
        default_factory=dict[ActorId, AllowlistProxy]
    )
    # Denials outlive their proxy: the executor reads them after the session ends.
    denied_history: list[DeniedConnection] = field(
        default_factory=list[DeniedConnection]
    )

    def local_ports(self, policy: EgressPolicy) -> tuple[int, ...]:
        """Ports on this machine the policy allows: the herd's own local services."""
        return tuple(
            port for rule in policy.allow if is_local(rule.host) for port in rule.ports
        )

    def start_proxy(self, agent: ActorId, policy: EgressPolicy) -> AllowlistProxy:
        previous = self.proxies.pop(agent, None)
        if previous is not None:
            self.retire(previous)
        else:
            pass
        self.work_directory.mkdir(parents=True, exist_ok=True)
        proxy = AllowlistProxy(
            agent=agent,
            policy=policy,
            clock=self.clock,
            unix_socket_path=(
                unix_socket_path_for(self.work_directory, agent)
                if self.layer_two == LAYER_LINUX
                else None
            ),
            denied_log=self.work_directory / f"{agent}-denied.tsv",
        )
        proxy.start()
        self.proxies[agent] = proxy
        return proxy

    def wrap(
        self,
        agent: ActorId,
        argv: tuple[str, ...],
        env: dict[str, str],
        policy: EgressPolicy,
    ) -> WrappedCommand:
        """Raise RuntimeError if the platform cannot enforce the policy below the proxy."""
        if self.layer_two == LAYER_NONE and not self.allow_unenforced:
            raise RuntimeError(
                "no OS-level egress enforcement on this platform (needs sandbox-exec on "
                "macOS or unprivileged user namespaces and mount on Linux); pass "
                "allow_unenforced=True for development only"
            )
        else:
            proxy = self.start_proxy(agent, policy)
            if self.layer_two == LAYER_MACOS:
                profile = self.work_directory / f"{agent}.sb"
                profile.write_text(
                    macos.build_profile(
                        (proxy.port, *self.local_ports(policy)),
                        self.protected_directories,
                    ),
                    encoding="utf-8",
                )
                wrapped = macos.wrap_argv(profile, argv)
                no_proxy = LOCAL_NO_PROXY
            elif self.layer_two == LAYER_LINUX:
                assert proxy.unix_socket_path is not None
                wrapped = (
                    self.python_executable,
                    "-m",
                    linux_namespace.MODULE,
                    "--proxy-socket",
                    str(proxy.unix_socket_path),
                    "--proxy-port",
                    str(proxy.port),
                    *(
                        argument
                        for directory in self.protected_directories
                        for argument in ("--protected", str(directory))
                    ),
                    "--",
                    *argv,
                )
                # The host's loopback does not exist in the namespace: everything,
                # the local tool server included, goes through the proxy.
                no_proxy = ""
            else:
                logger.warning(
                    f"EGRESS NOT ENFORCED for {agent}: only the proxy variables limit it, "
                    "and a process that ignores them can reach any host (development only)"
                )
                wrapped = argv
                no_proxy = LOCAL_NO_PROXY
            return WrappedCommand(
                argv=wrapped, env={**env, **proxy_environment(proxy.port, no_proxy)}
            )

    def retire(self, proxy: AllowlistProxy) -> None:
        proxy.stop()
        self.denied_history.extend(proxy.denied())

    def denied(self) -> tuple[DeniedConnection, ...]:
        live = [denied for proxy in self.proxies.values() for denied in proxy.denied()]
        return tuple(
            sorted(
                [*self.denied_history, *live],
                key=lambda denied: (
                    denied.timestamp_seconds,
                    denied.agent,
                    denied.host,
                ),
            )
        )

    def close(self) -> None:
        for proxy in self.proxies.values():
            self.retire(proxy)
        self.proxies.clear()
