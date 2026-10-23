"""The effective policy: the charter plus the herd's hosts, and how a host matches (D75)."""

import pytest

from tests.fixtures.egress import herd_hosts
from vlah.adapters.executor.sandbox.policy import (
    effective_policy,
    host_matches,
    is_allowed,
)
from vlah.core.types.params import EgressPolicy, HostRule

pytestmark = pytest.mark.egress


@pytest.mark.parametrize(
    ("rule", "host", "expected"),
    [
        ("api.example.com", "api.example.com", True),
        ("api.example.com", "API.EXAMPLE.COM.", True),
        ("api.example.com", "evil-api.example.com", False),
        ("*.example.com", "files.example.com", True),
        ("*.example.com", "deep.files.example.com", True),
        ("*.example.com", "example.com", False),
        ("*.example.com", "notexample.com", False),
        ("localhost", "127.0.0.1", True),
        ("127.0.0.1", "localhost", True),
        ("127.0.0.1", "127.0.0.2", False),
    ],
)
def test_host_matching(rule: str, host: str, expected: bool) -> None:
    assert host_matches(rule, host) is expected


def test_an_empty_charter_allows_only_the_herds_hosts() -> None:
    policy = effective_policy(EgressPolicy(), herd_hosts(8100, 8200))
    allowed = {(rule.host, rule.ports, rule.purpose) for rule in policy.allow}
    assert allowed == {
        ("127.0.0.1", (8100,), "tools"),
        ("api.example.com", (443,), "model_api"),
        ("127.0.0.1", (8200,), "testbed"),
    }
    assert is_allowed(policy, "localhost", 8100), "the tool server, MCP and the hook"
    assert is_allowed(policy, "127.0.0.1", 8200), "the slow-eval host, for tb fast"
    assert not is_allowed(policy, "127.0.0.1", 22)
    assert not is_allowed(policy, "api.example.com", 80), "the port is part of the rule"


def test_the_charters_context_sources_and_mirrors_are_added_once() -> None:
    charter = EgressPolicy(
        allow=(
            HostRule(host="*.pypi.org", purpose="mirror"),
            HostRule(host="arxiv.org", purpose="context"),
            HostRule(host="api.example.com", purpose="model_api"),
        )
    )
    policy = effective_policy(charter, herd_hosts(8100))
    hosts = [rule.host for rule in policy.allow]
    assert hosts == ["*.pypi.org", "arxiv.org", "api.example.com", "127.0.0.1"]
    assert is_allowed(policy, "files.pythonhosted.pypi.org", 443)
    assert not is_allowed(policy, "github.com", 443), "not written, so blocked"
