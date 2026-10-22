"""Claim and approach status machines, copied from v2.2 D9."""

import pytest

from vlah.core.types.ids import Role
from vlah.core.types.states import (
    INITIAL_STATUS,
    STATUS_KINDS,
    Subject,
    check_transition,
)

ALLOWED: list[tuple[Subject, str, str, Role]] = [
    ("result", "proposed", "verified:bench_fast", "eval_service"),
    ("result", "verified:bench_fast", "verified:bench_full", "eval_service"),
    ("result", "verified:bench_full", "refuted", "critic"),
    ("result", "verified:bench_full", "quarantined", "integrator"),
    ("result", "quarantined", "verified:bench_full", "human"),
    ("insight", "proposed", "corroborated", "indexer"),
    ("insight", "corroborated", "refuted", "critic"),
    ("dead_end", "proposed", "confirmed", "worker"),
    ("dead_end", "confirmed", "reinstated", "critic"),
    ("metric", "proposed", "calibrated", "eval_service"),
    ("metric", "calibrated", "deprecated", "critic"),
    ("question", "open", "answered", "librarian"),
    ("approach", "active", "frozen", "planner"),
    ("approach", "frozen", "dead_end", "planner"),
    ("approach", "dead_end", "active", "planner"),
]

DENIED: list[tuple[Subject, str, str, Role]] = [
    ("result", "proposed", "verified:bench_full", "worker"),
    ("result", "quarantined", "verified:bench_full", "integrator"),
    ("result", "proposed", "quarantined", "integrator"),
    ("result", "proposed", "verified:", "eval_service"),
    ("result", "proposed", "verified:*", "eval_service"),
    ("insight", "proposed", "corroborated", "worker"),
    ("dead_end", "proposed", "confirmed", "librarian"),
    ("metric", "proposed", "deprecated", "eval_service"),
    ("approach", "active", "dead_end", "planner"),
    ("approach", "active", "frozen", "worker"),
    ("audit", "final", "final", "human"),
    ("question", "open", "open", "human"),
]


@pytest.mark.parametrize(("subject", "current", "new", "role"), ALLOWED)
def test_spec_transitions_are_allowed(
    subject: Subject, current: str, new: str, role: Role
) -> None:
    decision = check_transition(subject, current, new, role)
    assert decision.is_allowed, decision.reason


@pytest.mark.parametrize(("subject", "current", "new", "role"), DENIED)
def test_other_transitions_are_denied(
    subject: Subject, current: str, new: str, role: Role
) -> None:
    assert not check_transition(subject, current, new, role).is_allowed


def test_every_subject_starts_in_one_of_its_own_statuses() -> None:
    assert set(INITIAL_STATUS) == set(STATUS_KINDS)
    for subject, status in INITIAL_STATUS.items():
        assert status in STATUS_KINDS[subject]
