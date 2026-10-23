"""The router's rules: a data file of {events, match, kind, ...} over a fixed set of predicates.

A rule changes without a code change (v2.2 section 8): the rules file chooses which
events a rule watches (events), which lease predicate it applies, whether it raises a
hard or soft interrupt or revokes another rule's, its priority, its text and the arm
flag that enables it. The predicates themselves are code, named below; a rules file
that names another is rejected when it loads.

Reason templates may use: {agent}, {approach}, {target}, {to}, {actor},
{event_reason}, {candidate}, {claim}, {baseline} and {files}.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, Protocol, Self

import yaml
from pydantic import Field, TypeAdapter, model_validator

from vlah.arms.config import ArmConfig
from vlah.core.indexer.api import Indexer
from vlah.core.types.base import FrozenModel
from vlah.core.types.claims import Claim
from vlah.core.types.events import BaselineAdvanced, Event, ResultReady
from vlah.core.types.ids import role_of_actor
from vlah.core.types.tasks import Lease

DEFAULT_RULES_FILE = "rules.default.yaml"
CLAIM_TRIGGER = "claim"
CLOSED_APPROACH_STATUSES = frozenset({"frozen", "dead_end"})
ZERO_BUDGET = "0"
AUTHORITATIVE_ROLES = frozenset({"human", "critic"})
ArmFlag = Literal["router_rule1", "router_rules_2_5", "router_rule6_baseline_push"]


class Rule(FrozenModel):
    id: str
    rule: int = Field(ge=1)
    # Not 'on': YAML reads that key as the boolean true.
    events: tuple[str, ...] = Field(min_length=1)
    match: str
    kind: Literal["hard", "soft", "revoke"]
    # Hard interrupts: the lower number is shown first.
    priority: int | None = None
    # Revoke entries: the id of the rule whose pending interrupts they withdraw.
    revokes: str | None = None
    enabled_by: ArmFlag | None = None
    reason: str = ""

    @model_validator(mode="after")
    def require_the_fields_of_its_kind(self) -> Self:
        if self.kind == "revoke" and self.revokes is None:
            raise ValueError(f"rule {self.id} revokes nothing")
        elif self.kind != "revoke" and self.revokes is not None:
            raise ValueError(f"rule {self.id} is not a revoke entry")
        elif self.kind == "hard" and self.priority is None:
            raise ValueError(f"rule {self.id} has no priority")
        elif self.kind != "revoke" and not self.reason:
            raise ValueError(f"rule {self.id} has no reason")
        else:
            return self

    def is_enabled(self, arm: ArmConfig) -> bool:
        return self.enabled_by is None or bool(getattr(arm, self.enabled_by))


@dataclass(frozen=True, kw_only=True)
class Trigger:
    """One thing a merged commit brought: an event, or a newly merged claim."""

    type: str
    # Unique per trigger within a run: the event's sequence number, or the commit and
    # claim for a claim, which has none.
    key: str
    event: Event | None
    claim: Claim | None


class Predicate(Protocol):
    def __call__(self, trigger: Trigger, lease: Lease, indexer: Indexer) -> bool: ...


def submitted_by_agent(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    event = trigger.event
    return (
        event is not None
        and isinstance(event.data, ResultReady)
        and event.data.submitter == lease.agent
    )


def targets_lease_claim(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    event = trigger.event
    return event is not None and event.target in lease.target_claims


def closes_lease_approach(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    """An approach frozen or abandoned, or an allocation that leaves it no slots."""
    event = trigger.event
    if event is None or event.target != lease.approach:
        return False
    elif event.type == "approach_status":
        return event.to in CLOSED_APPROACH_STATUSES
    else:
        return event.to == ZERO_BUDGET


def refutes_dependency_confirmed(
    trigger: Trigger, lease: Lease, indexer: Indexer
) -> bool:
    """A refutation of a dependency, from a human or the Critic or confirmed by a second
    source; a single-source refutation waits for the diff (D34)."""
    event = trigger.event
    if event is None or event.to != "refuted" or event.target not in lease.depends_on:
        return False
    elif event.role in AUTHORITATIVE_ROLES:
        return True
    else:
        return indexer.claim_state(event.target).is_cascaded_refuted


def reinstates_dependency(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    """A dependency's refutation withdrawn: reinstated by a human or the Critic, or
    a human moving the claim to any status but refuted (D34)."""
    event = trigger.event
    if event is None or event.target not in lease.depends_on:
        return False
    elif event.type == "reinstated":
        return event.role in AUTHORITATIVE_ROLES
    else:
        return event.role == "human" and event.to != "refuted"


def human_claim_on_approach(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    claim = trigger.claim
    return (
        claim is not None
        and role_of_actor(claim.author) == "human"
        and claim.approach == lease.approach
    )


def changed_files(trigger: Trigger) -> frozenset[str]:
    event = trigger.event
    if event is not None and isinstance(event.data, BaselineAdvanced):
        return frozenset(event.data.files_changed)
    else:
        return frozenset()


def touches_lease_files(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    return bool(changed_files(trigger) & set(lease.files_touched))


def misses_lease_files(trigger: Trigger, lease: Lease, indexer: Indexer) -> bool:
    event = trigger.event
    return (
        event is not None
        and isinstance(event.data, BaselineAdvanced)
        and not touches_lease_files(trigger, lease, indexer)
    )


PREDICATES: Mapping[str, Predicate] = {
    "submitted_by_agent": submitted_by_agent,
    "targets_lease_claim": targets_lease_claim,
    "closes_lease_approach": closes_lease_approach,
    "refutes_dependency_confirmed": refutes_dependency_confirmed,
    "reinstates_dependency": reinstates_dependency,
    "human_claim_on_approach": human_claim_on_approach,
    "touches_lease_files": touches_lease_files,
    "misses_lease_files": misses_lease_files,
}


class SafeFields(dict[str, str]):
    """A template field nobody filled renders as empty instead of failing."""

    def __missing__(self, key: str) -> str:
        return ""


def reason_fields(trigger: Trigger, lease: Lease) -> SafeFields:
    event = trigger.event
    claim = trigger.claim
    fields = SafeFields(agent=lease.agent, approach=lease.approach)
    if event is not None:
        fields.update(
            target=event.target or "",
            to=event.to or "",
            actor=event.actor,
            event_reason=event.reason or "",
        )
        if isinstance(event.data, ResultReady):
            fields["candidate"] = event.data.candidate
        elif isinstance(event.data, BaselineAdvanced):
            fields["baseline"] = str(event.data.baseline)
            fields["files"] = ", ".join(
                sorted(changed_files(trigger) & set(lease.files_touched))
            )
        else:
            pass
    else:
        pass
    if claim is not None:
        fields["claim"] = claim.id
    else:
        pass
    return fields


def render_reason(rule: Rule, trigger: Trigger, lease: Lease) -> str:
    return rule.reason.format_map(reason_fields(trigger, lease)).strip()


def parse_rules(text: str) -> tuple[Rule, ...]:
    """Rules from a rules file.

    An unknown predicate, a repeated id or a revoke entry naming no rule in the
    file is an error.
    """
    data: object = yaml.safe_load(text)
    rules = TypeAdapter(tuple[Rule, ...]).validate_python(data)
    unknown = sorted({rule.match for rule in rules} - PREDICATES.keys())
    ids = [rule.id for rule in rules]
    unknown_revoked = sorted(
        {rule.revokes for rule in rules if rule.revokes is not None} - set(ids)
    )
    if unknown:
        raise ValueError(
            f"unknown rule predicates {unknown}; known: {sorted(PREDICATES)}"
        )
    elif len(ids) != len(set(ids)):
        raise ValueError(f"rule ids repeat: {ids}")
    elif unknown_revoked:
        raise ValueError(f"revoked rules {unknown_revoked} are not in the file")
    else:
        return rules


def default_rules() -> tuple[Rule, ...]:
    return parse_rules(
        files("vlah.core.router")
        .joinpath(DEFAULT_RULES_FILE)
        .read_text(encoding="utf-8")
    )
