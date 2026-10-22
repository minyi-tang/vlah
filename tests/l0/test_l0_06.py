"""L0-06 (v2.2 §14.1). Owner: W1-02 (#12).

A single-source refuted → no cascade; a Critic refuted → cascades to everything that depends_on it; reinstated → undoes the cascade (D34).
"""

import pytest

from tests.fixtures.indexer import CRITIC, EVAL, make_indexer
from vlah.core.types.ids import ActorId

pytestmark = [pytest.mark.l0, pytest.mark.indexer]

WORKER = ActorId("worker-01")


def test_l0_06_refuted_cascade_rules() -> None:
    """A single-source refuted → no cascade; a Critic refuted → cascades to everything that depends_on it; reinstated → undoes the cascade (D34)."""
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x"})
    base = harness.result_claim("aa01")
    built_on = harness.claim(
        WORKER, "insight", "builds on the result", depends_on=(base,)
    )
    further = harness.claim(WORKER, "question", "and on that", depends_on=(built_on,))

    def cascaded() -> list[bool]:
        return [
            harness.indexer.claim_state(claim).is_cascaded_refuted
            for claim in (base, built_on, further)
        ]

    harness.status(EVAL, base, "refuted", "proposed")
    assert harness.indexer.claim_state(base).status == "refuted"
    assert cascaded() == [False, False, False], "one source, no cascade"

    other = harness.result_claim("aa01")
    dependent = harness.claim(
        WORKER, "insight", "builds on the other", depends_on=(other,)
    )
    harness.status(CRITIC, other, "refuted", "proposed")
    assert [
        harness.indexer.claim_state(claim).is_cascaded_refuted
        for claim in (other, dependent)
    ] == [True, True], "the Critic's refutation cascades"

    harness.event(CRITIC, "reinstated", target=other, reason="reproduced after all")
    assert harness.indexer.claim_state(other).status == "proposed"
    assert [
        harness.indexer.claim_state(claim).is_cascaded_refuted
        for claim in (other, dependent)
    ] == [False, False], "reinstated undoes it"
    assert cascaded() == [False, False, False]
