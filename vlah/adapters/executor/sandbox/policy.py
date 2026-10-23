"""The effective egress policy, and whether a destination is on it (v2.2 D75).

effective_policy adds the herd's own hosts to the charter's allowlist: the tool
server (MCP and the interrupt hook), the slow-eval host (tb fast posts events to
it, TB section 11.3) and the model API. An empty charter allowlist means only
those. The arm never appears here: egress is the same in every arm (D75).

A rule's host is exact or "*.suffix"; a host matches by name, so a worker cannot
reach a blocked host by its address unless the address itself is listed.
"""

from vlah.adapters.executor.sandbox.api import HerdHosts
from vlah.core.types.params import EgressPolicy, HostRule

HTTPS_PORT = 443
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_local(host: str) -> bool:
    return host.lower() in LOCAL_HOSTS


def host_matches(rule_host: str, host: str) -> bool:
    wanted = rule_host.lower()
    actual = host.lower().rstrip(".")
    if wanted.startswith("*."):
        suffix = wanted[1:]
        return actual.endswith(suffix) and actual != suffix.lstrip(".")
    elif is_local(wanted) and is_local(actual):
        return True
    else:
        return actual == wanted


def is_allowed(policy: EgressPolicy, host: str, port: int) -> bool:
    return any(
        host_matches(rule.host, host) and port in rule.ports for rule in policy.allow
    )


def herd_rules(herd_hosts: HerdHosts) -> tuple[HostRule, ...]:
    rules = [
        HostRule(
            host=herd_hosts.tool_server_host,
            ports=(herd_hosts.tool_server_port,),
            purpose="tools",
        ),
        HostRule(
            host=herd_hosts.model_api_host, ports=(HTTPS_PORT,), purpose="model_api"
        ),
    ]
    if herd_hosts.slow_eval_host is not None:
        host, _, port_text = herd_hosts.slow_eval_host.partition(":")
        ports = (int(port_text),) if port_text else (HTTPS_PORT,)
        rules.append(HostRule(host=host, ports=ports, purpose="testbed"))
    else:
        pass
    return tuple(rules)


def effective_policy(
    charter_policy: EgressPolicy, herd_hosts: HerdHosts
) -> EgressPolicy:
    """The charter's allowlist plus the herd's own hosts; never reads files."""
    merged = list(charter_policy.allow)
    for rule in herd_rules(herd_hosts):
        if rule not in merged:
            merged.append(rule)
        else:
            pass
    return EgressPolicy(allow=tuple(merged))
