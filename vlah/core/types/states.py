"""Status machines for claims and approaches, one per type, as data (v2.2 D9).

A result is verified once per ladder level, so its status is "verified:<source id>".
The order of ladder levels is the eval client's concern; here any verified level
counts as the same kind of status.
"""

from dataclasses import dataclass
from typing import Literal

from vlah.core.types.ids import WRITE_PRIORITY, Role

ClaimType = Literal["result", "insight", "dead_end", "metric", "question", "audit"]
Subject = Literal[
    "result", "insight", "dead_end", "metric", "question", "audit", "approach"
]

VERIFIED_PREFIX = "verified:"
VERIFIED_AT_ANY_LEVEL = "verified:*"

ALL_ROLES: frozenset[Role] = frozenset(WRITE_PRIORITY)

INITIAL_STATUS: dict[Subject, str] = {
    "result": "proposed",
    "insight": "proposed",
    "dead_end": "proposed",
    "metric": "proposed",
    "question": "open",
    "audit": "final",
    "approach": "active",
}

STATUS_KINDS: dict[Subject, frozenset[str]] = {
    "result": frozenset({"proposed", VERIFIED_AT_ANY_LEVEL, "refuted", "quarantined"}),
    "insight": frozenset({"proposed", "corroborated", "refuted"}),
    "dead_end": frozenset({"proposed", "confirmed", "reinstated"}),
    "metric": frozenset({"proposed", "calibrated", "deprecated"}),
    "question": frozenset({"open", "answered"}),
    "audit": frozenset({"final"}),
    "approach": frozenset({"active", "frozen", "dead_end"}),
}


@dataclass(frozen=True, kw_only=True)
class TransitionRule:
    subject: Subject
    from_kinds: frozenset[str]
    to_kind: str
    roles: frozenset[Role]


# A human may make any transition except out of a terminal status; that is not
# listed here (D9: "human: anything").
TRANSITION_RULES: tuple[TransitionRule, ...] = (
    TransitionRule(
        subject="result",
        from_kinds=frozenset({"proposed", VERIFIED_AT_ANY_LEVEL}),
        to_kind=VERIFIED_AT_ANY_LEVEL,
        roles=frozenset({"eval_service"}),
    ),
    TransitionRule(
        subject="result",
        from_kinds=frozenset({"proposed", VERIFIED_AT_ANY_LEVEL}),
        to_kind="refuted",
        roles=frozenset({"eval_service", "critic"}),
    ),
    TransitionRule(
        subject="result",
        from_kinds=frozenset({VERIFIED_AT_ANY_LEVEL}),
        to_kind="quarantined",
        roles=frozenset({"integrator"}),
    ),
    TransitionRule(
        subject="insight",
        from_kinds=frozenset({"proposed"}),
        to_kind="corroborated",
        roles=frozenset({"indexer", "critic"}),
    ),
    TransitionRule(
        subject="insight",
        from_kinds=frozenset({"proposed", "corroborated"}),
        to_kind="refuted",
        roles=frozenset({"critic"}),
    ),
    # A worker confirms a dead end by reproducing it; whether that worker is
    # independent of the author is checked by the indexer, not here.
    TransitionRule(
        subject="dead_end",
        from_kinds=frozenset({"proposed"}),
        to_kind="confirmed",
        roles=frozenset({"critic", "worker"}),
    ),
    TransitionRule(
        subject="dead_end",
        from_kinds=frozenset({"proposed", "confirmed"}),
        to_kind="reinstated",
        roles=frozenset({"critic"}),
    ),
    TransitionRule(
        subject="metric",
        from_kinds=frozenset({"proposed"}),
        to_kind="calibrated",
        roles=frozenset({"eval_service", "critic"}),
    ),
    TransitionRule(
        subject="metric",
        from_kinds=frozenset({"calibrated"}),
        to_kind="deprecated",
        roles=frozenset({"eval_service", "critic"}),
    ),
    TransitionRule(
        subject="question",
        from_kinds=frozenset({"open"}),
        to_kind="answered",
        roles=ALL_ROLES,
    ),
    TransitionRule(
        subject="approach",
        from_kinds=frozenset({"active"}),
        to_kind="frozen",
        roles=frozenset({"planner"}),
    ),
    TransitionRule(
        subject="approach",
        from_kinds=frozenset({"frozen"}),
        to_kind="dead_end",
        roles=frozenset({"planner"}),
    ),
    TransitionRule(
        subject="approach",
        from_kinds=frozenset({"frozen", "dead_end"}),
        to_kind="active",
        roles=frozenset({"planner"}),
    ),
)

TERMINAL_SUBJECTS: frozenset[Subject] = frozenset({"audit"})


@dataclass(frozen=True, kw_only=True)
class TransitionDecision:
    is_allowed: bool
    reason: str


def status_kind(subject: Subject, status: str) -> str | None:
    """Map a status to its kind ("verified:x" becomes "verified:*"); None if invalid."""
    if subject == "result" and status.startswith(VERIFIED_PREFIX):
        level = status.removeprefix(VERIFIED_PREFIX)
        if level and level != "*":
            return VERIFIED_AT_ANY_LEVEL
        else:
            return None
    elif status in STATUS_KINDS[subject] and status != VERIFIED_AT_ANY_LEVEL:
        return status
    else:
        return None


def check_transition(
    subject: Subject, current_status: str, new_status: str, role: Role
) -> TransitionDecision:
    """Decide whether role may move a claim or approach from one status to another."""
    current_kind = status_kind(subject, current_status)
    new_kind = status_kind(subject, new_status)
    if current_kind is None:
        return TransitionDecision(
            is_allowed=False, reason=f"{current_status!r} is not a {subject} status"
        )
    elif new_kind is None:
        return TransitionDecision(
            is_allowed=False, reason=f"{new_status!r} is not a {subject} status"
        )
    elif current_status == new_status:
        return TransitionDecision(is_allowed=False, reason="status is unchanged")
    elif subject in TERMINAL_SUBJECTS:
        return TransitionDecision(
            is_allowed=False, reason=f"{subject} status is terminal"
        )
    elif role == "human":
        return TransitionDecision(
            is_allowed=True, reason="a human may make any transition"
        )
    elif any(
        rule.subject == subject
        and current_kind in rule.from_kinds
        and rule.to_kind == new_kind
        and role in rule.roles
        for rule in TRANSITION_RULES
    ):
        return TransitionDecision(is_allowed=True, reason="allowed by D9")
    else:
        return TransitionDecision(
            is_allowed=False,
            reason=f"{role} may not move a {subject} from {current_status} to {new_status}",
        )
