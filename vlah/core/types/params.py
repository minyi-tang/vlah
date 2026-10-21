"""Charter files that code reads: params, gates, egress, limits and target (v2.2 sections 5, 9, 10).

Every default here is the spec's default and is written only here; components take
these values as parameters instead of keeping their own copies.
"""

import re
from typing import Literal

import yaml
from pydantic import Field

from vlah.core.types.base import FrozenModel


class Params(FrozenModel):
    """charter/params.yaml. One instance is shared by every arm of a comparison (D58)."""

    diff_truncate_tokens: int = Field(default=500, ge=1)  # D18
    digest_max_tokens: int = Field(default=2000, ge=1)  # D14
    lease_ttl_seconds: float = Field(default=7200.0, gt=0)  # D32
    heartbeat_interval_seconds: float = Field(default=30.0, gt=0)  # D32
    interrupt_ack_timeout_seconds: float = Field(default=300.0, gt=0)  # D60
    consecutive_failures_before_refresh: int = Field(default=3, ge=1)  # D19
    context_restart_fraction: float = Field(default=0.70, gt=0, lt=1)  # D77
    restart_tool_call_count: int = Field(default=20, ge=0)  # D77
    similarity_threshold: float = Field(default=0.90, ge=0, le=1)
    t1_per_worker_per_day: int = Field(default=20, ge=0)  # D40
    verdict_repeat_count: int = Field(default=5, ge=1)  # D66
    holdout_run_cap: int = Field(default=10, ge=0)  # D66
    attention_minutes_per_day: int = Field(default=30, ge=0)  # D53
    slots_per_approach: dict[str, int] = {}  # fixed during H1 (D49, D42 rule 1)
    # Integrator: identical in every arm (D74, round 30). Names follow the testbed
    # (Herd Testbed section 11.3, 11.8.1).
    accept_rule: Literal["default", "strict"] = "default"
    accept_min_standard_errors: float = Field(default=1.0, ge=0)
    interaction_sigma_multiple: float = Field(default=2.0, ge=0)  # times sqrt(2)
    spot_check_every_merges: int = Field(default=2, ge=1)  # D72
    spot_check_sigma_multiple: float = Field(default=2.5, ge=0)  # D72
    spot_check_reserved_for_bisection: int = Field(default=3, ge=0)  # D72
    guard_every_merges: int = Field(default=3, ge=1)  # D74
    quota_integrator_total: int = Field(default=16, ge=0)
    quota_integrator_per_hour: int = Field(default=1, ge=0)


GateStage = Literal["pre_submit", "post_result", "guard"]


class GateSpec(FrozenModel):
    """One hard constraint in charter/gates.yaml; it passes or fails, it is never a score (D54).

    The eval client runs pre_submit and post_result gates; the Integrator runs guard
    gates in batches every guard_every_merges merges.
    """

    name: str
    command: str
    when: tuple[GateStage, ...] = Field(min_length=1)


class HostRule(FrozenModel):
    """One allowed destination: an exact host or "*.suffix", with ports."""

    host: str
    ports: tuple[int, ...] = (443,)
    purpose: Literal["model_api", "tools", "context", "mirror", "testbed"]


class EgressPolicy(FrozenModel):
    """Hosts a worker sandbox may reach; everything else is blocked (D75)."""

    allow: tuple[HostRule, ...] = ()


class Limits(FrozenModel):
    """Budget limits from charter/constraints.md; tokens are never limited (P10)."""

    eval_machine_hours: float = Field(gt=0)
    days: float = Field(gt=0)
    runs: dict[str, int] = {}


class Target(FrozenModel):
    """The top-level score that counts as reaching the goal (charter/progress.md)."""

    source: str
    score: float
    direction: Literal["higher", "lower"]


FENCED_YAML_BLOCK = r"^```yaml {name}\s*\n(.*?)^```\s*$"


def fenced_yaml_block(markdown: str, name: str) -> object | None:
    """Content of the first ```yaml <name> block in a markdown file, or None."""
    block_pattern = re.compile(
        FENCED_YAML_BLOCK.format(name=re.escape(name)), re.M | re.S
    )
    block_match = block_pattern.search(markdown)
    if block_match is None:
        return None
    else:
        loaded: object = yaml.safe_load(block_match.group(1))
        return loaded


def parse_limits(constraints_markdown: str) -> Limits:
    block = fenced_yaml_block(constraints_markdown, "limits")
    if block is None:
        raise ValueError("charter/constraints.md has no ```yaml limits block")
    else:
        return Limits.model_validate(block)


def parse_target(progress_markdown: str) -> Target | None:
    block = fenced_yaml_block(progress_markdown, "target")
    if block is None:
        return None
    else:
        return Target.model_validate(block)
