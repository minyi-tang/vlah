"""L0-02 (v2.2 §14.1). Owner: W1-02 (#12).

Write claim → merge produces seq → rebuild the Indexer from zero twice → the two states are identical and equal the incremental state (D56).
"""

import pytest

from tests.fixtures.indexer import CRITIC, EVAL, TOP_LEVEL, make_indexer
from vlah.core.types.claims import EvidenceRef
from vlah.core.types.ids import ActorId

pytestmark = [pytest.mark.l0, pytest.mark.indexer]

WORKER = ActorId("worker-01")


def test_l0_02_replay_is_deterministic() -> None:
    """Write claim → merge produces seq → rebuild the Indexer from zero twice → the two states are identical and equal the incremental state (D56)."""
    harness = make_indexer()
    insight = harness.claim(WORKER, "insight", "caching halves the misses")
    harness.candidate(WORKER, "aa01", {"solver.py": "cache = {}\n"})
    harness.candidate(WORKER, "bb02", {"solver.py": "cache = {}\nmore\n"})
    harness.lineage("bb02", ("aa01",))
    first = harness.claim(
        EVAL,
        "result",
        "Result of aa01.",
        evidence=(EvidenceRef(candidate="candidates/aa01/result.json"),),
        depends_on=(insight,),
    )
    head_before = harness.blackboard.head_sequence()
    harness.status(EVAL, first, f"verified:{TOP_LEVEL}", "proposed")
    assert (
        harness.blackboard.head_sequence() == head_before + 1
    ), "the merge produced a seq"
    second = harness.result_claim("bb02")
    harness.status(CRITIC, first, "refuted", f"verified:{TOP_LEVEL}")
    harness.claim(WORKER, "question", "does it hold on cold caches?")

    incremental = harness.indexer.fingerprint()
    harness.indexer.rebuild()
    once = harness.indexer.fingerprint()
    harness.indexer.rebuild()
    twice = harness.indexer.fingerprint()
    assert once == twice == incremental
    assert harness.indexer.claim_state(second).is_cascaded_refuted, "lineage replayed"
