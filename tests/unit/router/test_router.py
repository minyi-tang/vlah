"""The router's rules 1-6, arm gating, rule-file edits, priority and the H3 fallback (v2.2 section 8)."""

import time
from importlib.resources import files

import pytest

from tests.fixtures.router import (
    APPROACH,
    OTHER,
    WORKER,
    RouterHarness,
    approach_status,
    baseline_advanced,
    claim_status,
    commit,
    event,
    human_claim,
    make_router,
    result_ready,
)
from vlah.arms.config import PRESETS, ArmName
from vlah.core.blackboard.api import MergedCommit
from vlah.core.router.router import interrupt_key
from vlah.core.router.rules import parse_rules
from vlah.core.types.params import Params

pytestmark = pytest.mark.router

TARGET = "c-worker-01-00001"
DEPENDENCY = "c-worker-02-00007"
DEFAULT_RULES_TEXT = (
    files("vlah.core.router").joinpath("rules.default.yaml").read_text(encoding="utf-8")
)


def leased(arm: ArmName, rules_text: str | None = None) -> RouterHarness:
    harness = make_router(
        PRESETS[arm], rules=parse_rules(rules_text) if rules_text else None
    )
    harness.lease(
        target_claims=(TARGET,),
        depends_on=(DEPENDENCY,),
        files_touched=("solver.py",),
    )
    return harness


def fired(harness: RouterHarness, merged_commit: MergedCommit) -> list[tuple[int, str]]:
    return [
        (interrupt.rule, interrupt.kind)
        for interrupt in harness.router.on_commit(merged_commit)
    ]


@pytest.mark.parametrize(
    ("description", "merged_commit", "expected"),
    [
        ("1: my result arrived", commit(result_ready(1, WORKER)), [(1, "hard")]),
        ("1: someone else's result", commit(result_ready(1, OTHER)), []),
        (
            "2: my target changed",
            commit(claim_status(1, TARGET, "corroborated")),
            [(2, "hard")],
        ),
        (
            "2: another claim changed",
            commit(claim_status(1, "c-x-00001", "refuted")),
            [],
        ),
        (
            "3: my approach frozen",
            commit(approach_status(1, APPROACH, "frozen")),
            [(3, "hard")],
        ),
        (
            "3: my approach reinstated",
            commit(approach_status(1, APPROACH, "active")),
            [],
        ),
        (
            "3: another approach frozen",
            commit(approach_status(1, "a-02", "frozen")),
            [],
        ),
        (
            "3: my approach has no budget",
            commit(
                event(
                    1,
                    "allocation",
                    actor="planner",
                    role="planner",
                    target=APPROACH,
                    to="0",
                )
            ),
            [(3, "hard")],
        ),
        (
            "4: dependency refuted by the Critic",
            commit(claim_status(1, DEPENDENCY, "refuted")),
            [(4, "hard")],
        ),
        (
            "4: dependency refuted by one worker",
            commit(
                claim_status(1, DEPENDENCY, "refuted", actor="worker-03", role="worker")
            ),
            [],
        ),
        (
            "5: a human wrote about my approach",
            commit(claims=(human_claim(APPROACH),)),
            [(5, "hard")],
        ),
        (
            "5: a human wrote about another approach",
            commit(claims=(human_claim("a-02"),)),
            [],
        ),
        (
            "6: the baseline touched my files",
            commit(baseline_advanced(1, ("solver.py", "x.py"))),
            [(6, "hard")],
        ),
        (
            "6: the baseline missed my files",
            commit(baseline_advanced(1, ("other.py",))),
            [(6, "soft")],
        ),
        (
            "7: anything else",
            commit(event(1, "plateau", actor="planner", role="planner")),
            [],
        ),
    ],
)
def test_each_rule_fires_on_its_event_and_only_then(
    description: str, merged_commit: MergedCommit, expected: list[tuple[int, str]]
) -> None:
    assert fired(leased("S2"), merged_commit) == expected, description


def test_a_single_source_refutation_waits_until_a_second_source_confirms() -> None:
    """D34: one worker's refutation only goes into the diff; confirmation interrupts."""
    harness = leased("S2")
    refutation = claim_status(
        1, DEPENDENCY, "refuted", actor="worker-03", role="worker"
    )
    assert fired(harness, commit(refutation)) == []
    assert harness.router.soft_notes(WORKER) == (), "not even a soft note"
    harness.indexer.cascaded_refuted.add(DEPENDENCY)
    confirmation = claim_status(
        2, DEPENDENCY, "refuted", actor="worker-04", role="worker"
    )
    assert fired(harness, commit(confirmation)) == [(4, "hard")]


@pytest.mark.parametrize(
    ("arm", "expected_rules"),
    [("B1", {1}), ("S1", {1, 6}), ("S2", {1, 2, 3, 4, 5, 6})],
)
def test_arm_presets_gate_the_rules(arm: ArmName, expected_rules: set[int]) -> None:
    harness = leased(arm)
    everything = commit(
        result_ready(1, WORKER),
        claim_status(2, TARGET, "refuted"),
        approach_status(3, APPROACH, "dead_end"),
        claim_status(4, DEPENDENCY, "refuted"),
        baseline_advanced(5, ("solver.py",)),
        claims=(human_claim(APPROACH),),
    )
    assert {rule for rule, _ in fired(harness, everything)} == expected_rules


def test_editing_the_rules_file_changes_behaviour_without_code() -> None:
    edited = DEFAULT_RULES_TEXT.replace(
        "  match: misses_lease_files\n  kind: soft",
        "  match: misses_lease_files\n  kind: hard",
    )
    without_rule_2 = "\n\n".join(
        block
        for block in DEFAULT_RULES_TEXT.split("\n\n")
        if "id: target-changed" not in block
    )
    assert edited != DEFAULT_RULES_TEXT
    assert fired(leased("S2", edited), commit(baseline_advanced(1, ("other.py",)))) == [
        (6, "hard")
    ]
    assert (
        fired(leased("S2", without_rule_2), commit(claim_status(1, TARGET, "refuted")))
        == []
    )


def test_a_rules_file_naming_an_unknown_predicate_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown rule predicates"):
        parse_rules(DEFAULT_RULES_TEXT.replace("submitted_by_agent", "always"))


def test_the_most_urgent_interrupt_is_shown_and_the_rest_counted() -> None:
    harness = leased("S2")
    harness.router.on_commit(commit(result_ready(1, WORKER)))
    harness.router.on_commit(commit(claims=(human_claim(APPROACH),)))
    harness.router.on_commit(commit(baseline_advanced(2, ("solver.py",))))
    shown = harness.router.check(WORKER)
    assert shown is not None and shown.rule == 5, "human input comes first (P6)"
    assert harness.router.pending_count(WORKER) == 2
    assert harness.store.get(interrupt_key(WORKER)) is not None


def test_each_event_interrupts_an_agent_once() -> None:
    harness = leased("S2")
    merged = commit(result_ready(1, WORKER))
    assert len(harness.router.on_commit(merged)) == 1
    assert harness.router.on_commit(merged) == (), "a replayed commit is deduplicated"


def test_acknowledging_clears_only_the_interrupt_shown() -> None:
    harness = leased("S2")
    (interrupt,) = harness.router.on_commit(commit(result_ready(1, WORKER)))
    harness.router.acknowledge(WORKER, "i-someone-else")
    assert harness.router.check(WORKER) == interrupt, "a foreign id does nothing"
    harness.clock.advance(12.0)
    harness.router.acknowledge(WORKER, interrupt.id)
    assert harness.router.check(WORKER) is None
    assert harness.router.stats().latencies_seconds == (12.0,)


def test_overdue_returns_unacknowledged_interrupts_past_the_timeout_once() -> None:
    timeout = Params().interrupt_ack_timeout_seconds
    harness = leased("S2")
    harness.lease(agent=OTHER, approach="a-02")
    (mine,) = harness.router.on_commit(commit(result_ready(1, WORKER)))
    (theirs,) = harness.router.on_commit(commit(result_ready(2, OTHER)))
    harness.router.acknowledge(OTHER, theirs.id)
    harness.clock.advance(timeout - 1)
    harness.router.on_commit(commit(claims=(human_claim(APPROACH),)))
    assert harness.router.overdue(harness.clock.now_seconds()) == ()
    harness.clock.advance(1)
    (due,) = harness.router.overdue(harness.clock.now_seconds())
    assert due.agent == WORKER and due.rule == 5, "the oldest write sets the clock"
    assert harness.router.overdue(harness.clock.now_seconds() + timeout) == ()
    assert harness.router.check(WORKER) is None, "the restart carries it now"
    assert harness.router.stats().overdue_fraction == 0.5
    assert mine.agent == WORKER


def test_check_is_one_read_and_fast() -> None:
    harness = leased("S2")
    harness.router.on_commit(commit(result_ready(1, WORKER)))
    started = time.perf_counter()
    for _ in range(1000):
        assert harness.router.check(WORKER) is not None
    assert (time.perf_counter() - started) / 1000 < 0.005
