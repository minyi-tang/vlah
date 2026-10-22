"""The indexer: D9 transitions, D34 priority, cascades, diffs, search, corroboration (v2.2 D9, D18, D34)."""

from dataclasses import dataclass, field

import pytest

from tests.fixtures.indexer import CRITIC, EVAL, HUMAN, TOP_LEVEL, make_indexer
from vlah.arms.config import PRESETS
from vlah.core.blackboard.api import MergedCommit
from vlah.core.indexer.api import WorkingSet
from vlah.core.types.claims import EvidenceRef
from vlah.core.types.ids import ActorId

pytestmark = pytest.mark.indexer

WORKER = ActorId("worker-01")
OTHER = ActorId("worker-02")
THIRD = ActorId("worker-03")
VERIFIED = f"verified:{TOP_LEVEL}"


def test_transitions_follow_the_d9_table_and_its_roles() -> None:
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x = 1\n"})
    result = harness.result_claim("aa01")
    insight = harness.claim(WORKER, "insight", "caching halves misses")
    dead_end = harness.claim(WORKER, "dead_end", "unrolling did nothing")
    harness.status(OTHER, result, VERIFIED, "proposed")
    harness.status(OTHER, insight, "refuted", "proposed")
    harness.status(WORKER, dead_end, "confirmed", "proposed")
    states = {
        claim: harness.indexer.claim_state(claim).status
        for claim in (result, insight, dead_end)
    }
    assert states == {
        result: "proposed",
        insight: "proposed",
        dead_end: "proposed",
    }, "a worker may not verify, refute an insight, or confirm its own dead end"
    harness.status(EVAL, result, VERIFIED, "proposed")
    harness.status(CRITIC, insight, "refuted", "proposed")
    harness.status(OTHER, dead_end, "confirmed", "proposed")
    states = {
        claim: harness.indexer.claim_state(claim).status
        for claim in (result, insight, dead_end)
    }
    assert states == {result: VERIFIED, insight: "refuted", dead_end: "confirmed"}


def test_a_lower_priority_role_cannot_overturn_a_higher_one() -> None:
    """D34: human > Critic > eval service > ... ; the higher one wins."""
    harness = make_indexer()
    metric = harness.claim(WORKER, "metric", "latency p99")
    harness.status(CRITIC, metric, "calibrated", "proposed")
    harness.status(EVAL, metric, "deprecated", "calibrated")
    assert harness.indexer.claim_state(metric).status == "calibrated"
    harness.status(HUMAN, metric, "deprecated", "calibrated")
    assert harness.indexer.claim_state(metric).status == "deprecated"


def test_a_refuted_candidates_descendants_are_marked() -> None:
    harness = make_indexer()
    for hash_value in ("aa01", "bb02", "cc03"):
        harness.candidate(WORKER, hash_value, {"solver.py": hash_value})
    harness.lineage("bb02", ("aa01",))
    harness.lineage("cc03", ("bb02",))
    parent, child, grandchild = (
        harness.result_claim(h) for h in ("aa01", "bb02", "cc03")
    )
    harness.status(CRITIC, parent, "refuted", "proposed")
    cascaded = [
        harness.indexer.claim_state(claim).is_cascaded_refuted
        for claim in (parent, child, grandchild)
    ]
    assert cascaded == [True, True, True]
    assert (
        harness.indexer.claim_state(child).status == "proposed"
    ), "marked, not rewritten"


def test_one_source_twice_is_still_one_source() -> None:
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x"})
    result = harness.result_claim("aa01")
    harness.status(EVAL, result, "refuted", "proposed")
    harness.status(EVAL, result, "refuted", "refuted")
    assert not harness.indexer.claim_state(result).is_cascaded_refuted
    harness.status(CRITIC, result, "refuted", "refuted")
    assert harness.indexer.claim_state(
        result
    ).is_cascaded_refuted, "a second source confirms"


def test_the_diff_ranks_truncates_and_ends_with_the_rest_count() -> None:
    harness = make_indexer()
    harness.candidate(OTHER, "aa01", {"solver.py": "x"})
    other_insight = harness.claim(
        OTHER, "insight", "another approach found a trick", approach="a-02"
    )
    dead_end = harness.claim(OTHER, "dead_end", "this approach's unrolling failed")
    result = harness.result_claim("aa01")
    target = harness.claim(THIRD, "question", "is the cache line 64 bytes?")
    harness.claim(WORKER, "insight", "my own note")
    harness.status(EVAL, result, VERIFIED, "proposed")
    working_set = WorkingSet(approach="a-01", target_claims=(target,), recent_ids=())
    diff = harness.indexer.diff_since(0, working_set, WORKER, 500, PRESETS["S2"])
    assert [item.item_id for item in diff.items] == [
        target,
        result,
        dead_end,
        other_insight,
    ]
    assert "my own note" not in diff.rendered, "an agent's own claims are not news"
    assert diff.cursor == harness.blackboard.head_sequence()
    short = harness.indexer.diff_since(0, working_set, WORKER, 40, PRESETS["S2"])
    assert 0 < len(short.items) < 4 and short.omitted_count == 4 - len(short.items)
    assert short.rendered.splitlines()[-1] == f"{short.omitted_count} more, use search"
    assert (
        harness.indexer.diff_since(
            diff.cursor, working_set, WORKER, 500, PRESETS["S2"]
        ).items
        == ()
    )
    results_only = harness.indexer.diff_since(
        0, working_set, WORKER, 500, PRESETS["S1"]
    )
    assert [item.item_id for item in results_only.items] == [result]
    assert (
        harness.indexer.diff_since(0, working_set, WORKER, 500, PRESETS["B1"]).items
        == ()
    )


def test_a_claim_without_an_event_arrives_with_the_next_one() -> None:
    """A claim-only commit gets no sequence number; the next event's carries it."""
    harness = make_indexer()
    harness.candidate(OTHER, "aa01", {"solver.py": "x"})
    result = harness.result_claim("aa01")
    harness.status(EVAL, result, VERIFIED, "proposed")
    working_set = WorkingSet(approach="a-01", target_claims=(), recent_ids=())
    first = harness.indexer.diff_since(0, working_set, WORKER, 500, PRESETS["S2"])
    insight = harness.claim(OTHER, "insight", "late finding")
    waiting = harness.indexer.diff_since(
        first.cursor, working_set, WORKER, 500, PRESETS["S2"]
    )
    assert waiting.items == (), "not stamped yet"
    harness.status(CRITIC, result, "refuted", VERIFIED)
    arrived = harness.indexer.diff_since(
        first.cursor, working_set, WORKER, 500, PRESETS["S2"]
    )
    assert insight in [item.item_id for item in arrived.items]
    again = harness.indexer.diff_since(
        arrived.cursor, working_set, WORKER, 500, PRESETS["S2"]
    )
    assert again.items == (), "delivered once"


def test_search_is_scoped_by_arm() -> None:
    harness = make_indexer()
    harness.claim(OTHER, "insight", "the cache line trick halves misses")
    mine = harness.claim(WORKER, "insight", "my cache experiment")
    harness.candidate(OTHER, "aa01", {"solver.py": "cache = {}\n"})
    assert harness.indexer.search("cache", 5, WORKER, PRESETS["B1"]) == ()
    assert len(harness.indexer.search("cache", 5, WORKER, PRESETS["S2"])) == 3
    candidates_only = harness.indexer.search("cache", 5, WORKER, PRESETS["S1"])
    assert [hit.kind for hit in candidates_only] == ["candidate"]
    own = harness.indexer.search("cache", 5, WORKER, PRESETS["B0"])
    assert [hit.item_id for hit in own] == [mine]


def test_similar_candidates_compare_payloads_not_targets() -> None:
    """D69: the same change against a different baseline is still the same idea."""
    harness = make_indexer()
    harness.candidate(
        OTHER, "aa01", {"solver.py": "def total(xs):\n    return sum(xs)\n"}
    )
    (hit,) = harness.indexer.similar_candidates(
        "solver.py\ndef total(xs):\n    return sum(xs)\n", 1, WORKER
    )
    assert hit.item_id == "aa01" and hit.similarity > 0.99
    assert harness.indexer.similar_dead_ends("anything", 5, WORKER) == ()


def test_only_confirmed_dead_ends_are_presearched() -> None:
    harness = make_indexer()
    harness.candidate(OTHER, "aa01", {"solver.py": "unroll the loop eight times\n"})
    dead_end = harness.claim(
        OTHER,
        "dead_end",
        "unrolling eight times did nothing",
        evidence=(EvidenceRef(candidate="candidates/aa01/manifest.yaml"),),
    )
    assert harness.indexer.similar_dead_ends("unroll the loop", 5, WORKER) == ()
    harness.status(THIRD, dead_end, "confirmed", "proposed")
    (hit,) = harness.indexer.similar_dead_ends("unroll the loop eight times", 5, WORKER)
    assert hit.item_id == dead_end


def test_corroboration_is_written_once_across_rebuilds() -> None:
    harness = make_indexer()
    insight = harness.claim(OTHER, "insight", "cache-oblivious layouts help")
    for hash_value in ("aa01", "bb02"):
        harness.candidate(WORKER, hash_value, {"solver.py": hash_value})
    results = [
        harness.claim(
            EVAL,
            "result",
            f"Result of {hash_value}.",
            evidence=(EvidenceRef(candidate=f"candidates/{hash_value}/result.json"),),
            depends_on=(insight,),
        )
        for hash_value in ("aa01", "bb02")
    ]
    harness.status(EVAL, results[0], VERIFIED, "proposed")
    assert (
        harness.indexer.claim_state(insight).status == "proposed"
    ), "one is not enough"
    harness.status(EVAL, results[1], VERIFIED, "proposed")
    assert harness.indexer.claim_state(insight).status == "corroborated"
    harness.indexer.rebuild()
    harness.indexer.rebuild()
    indexer_events = harness.blackboard.list_paths("events/indexer/", None)
    assert len(indexer_events) == 1
    assert harness.indexer.claim_state(insight).status == "corroborated"
    assert harness.indexer.reuse_count(insight) == 2


def test_applying_a_commit_twice_changes_nothing() -> None:
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x"})
    result = harness.result_claim("aa01")
    harness.status(EVAL, result, VERIFIED, "proposed")
    before = harness.indexer.fingerprint()
    harness.indexer.rebuild()
    assert harness.indexer.fingerprint() == before


def test_the_dead_end_rewalk_metric_counts_top_level_runs_like_a_confirmed_dead_end() -> (
    None
):
    harness = make_indexer()
    harness.candidate(OTHER, "aa01", {"solver.py": "unroll the loop eight times\n"})
    dead_end = harness.claim(
        OTHER,
        "dead_end",
        "unrolling eight times did nothing",
        evidence=(EvidenceRef(candidate="candidates/aa01/manifest.yaml"),),
    )
    harness.status(THIRD, dead_end, "confirmed", "proposed")
    harness.candidate(WORKER, "bb02", {"solver.py": "unroll the loop eight times\n"})
    harness.event(
        EVAL,
        "result_ready",
        data={
            "candidate": "bb02",
            "claim_id": "c-eval_service-00009",
            "approach": "a-01",
            "submitter": WORKER,
            "submitter_checkpoint": None,
            "baseline": "baseline@0",
            "paired": {"mean": 0.0, "se": 0.0, "n_items": 1, "n_reps": 1},
            "per_item": "",
            "score": 0.0,
            "level": TOP_LEVEL,
        },
    )
    assert harness.indexer.metrics().dead_end_rewalk_count == 1


@dataclass(kw_only=True)
class DeliveredCommits:
    commits: list[MergedCommit] = field(default_factory=list[MergedCommit])

    def __call__(self, merged_commit: MergedCommit) -> None:
        self.commits.append(merged_commit)


def test_a_commit_delivered_twice_changes_nothing() -> None:
    """Delivery is at least once (#11): a second apply of the same commit is a no-op."""
    harness = make_indexer()
    delivered = DeliveredCommits()
    harness.blackboard.subscribe(delivered)
    harness.candidate(
        WORKER, "aa01", {"solver.py": "def total(xs):\n    return sum(xs)\n"}
    )
    result = harness.result_claim("aa01")
    harness.status(EVAL, result, VERIFIED, "proposed")
    before = harness.indexer.fingerprint()
    for merged_commit in delivered.commits:
        harness.indexer.apply(merged_commit)
    assert harness.indexer.fingerprint() == before
    (hit,) = harness.indexer.similar_candidates(
        "solver.py\ndef total(xs):\n    return sum(xs)\n", 1, OTHER
    )
    assert hit.similarity > 0.99
    assert harness.indexer.catalog.candidate_text("aa01") == (
        "solver.py\ndef total(xs):\n    return sum(xs)\n"
    ), "the payload text did not double"


def test_s2_search_lists_a_confirmed_dead_end_once() -> None:
    harness = make_indexer()
    dead_end = harness.claim(OTHER, "dead_end", "unrolling the cache loop did nothing")
    harness.status(THIRD, dead_end, "confirmed", "proposed")
    hits = harness.indexer.search("unrolling cache loop", 5, WORKER, PRESETS["S2"])
    assert [hit.item_id for hit in hits].count(dead_end) == 1
