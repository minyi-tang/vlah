"""The indexer: D9 transitions, D34 priority, cascades, diffs, search, corroboration (v2.2 D9, D18, D34)."""

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from tests.fixtures.herd import TOY_PUSH_TOKEN, MemoryMirrorStores
from tests.fixtures.indexer import (
    CRITIC,
    EVAL,
    HUMAN,
    TOP_LEVEL,
    IndexerHarness,
    make_indexer,
)
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import MemoryVectorIndex, MemoryVersionedStore
from vlah.arms.config import PRESETS
from vlah.core.blackboard.api import MergedCommit, WriteReceipt
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.indexer.api import WorkingSet
from vlah.core.indexer.indexer import INDEXER, StoreIndexer
from vlah.core.types.claims import EvidenceRef
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.ports.store import VectorHit

pytestmark = pytest.mark.indexer

WORKER = ActorId("worker-01")
OTHER = ActorId("worker-02")
THIRD = ActorId("worker-03")
INTEGRATOR = ActorId("integrator")
PLANNER = ActorId("planner-1")
VERIFIED = f"verified:{TOP_LEVEL}"
FAST = "verified:fast"
QUERY = "caching misses"
YIELD_COUNT = 20


class RejectingBlackboard(StoreBlackboard):
    """The real blackboard, rejecting the indexer's first write as the git store might."""

    rejections_left = 1

    def write(
        self, actor: ActorId, files: Mapping[str, bytes], message: str
    ) -> WriteReceipt:
        if actor == INDEXER and self.rejections_left:
            self.rejections_left -= 1
            return WriteReceipt(
                status="rejected", commit_id=None, sequence_numbers=(), violations=()
            )
        else:
            return super().write(actor, files, message)


@dataclass(kw_only=True)
class ParkedQueries:
    """A vector index whose query waits to be released, holding the index open."""

    inner: MemoryVectorIndex
    in_query: threading.Event
    release: threading.Event
    is_overlapped: bool = False

    def upsert(
        self, item_id: str, vector: tuple[float, ...], metadata: dict[str, str]
    ) -> None:
        if self.in_query.is_set():
            self.is_overlapped = True
        else:
            pass
        self.inner.upsert(item_id, vector, metadata)

    def query(
        self, vector: tuple[float, ...], limit: int, where: dict[str, str] | None
    ) -> tuple[VectorHit, ...]:
        self.in_query.set()
        self.release.wait()
        hits = self.inner.query(vector, limit, where)
        self.in_query.clear()
        return hits

    def delete(self, item_id: str) -> bool:
        return self.inner.delete(item_id)


class SignallingModel(MemoryModel):
    """Flags the embedding that runs while a query is parked: the writer's."""

    def __init__(self, in_query: threading.Event) -> None:
        super().__init__()
        self.in_query = in_query
        self.writer_embedded = threading.Event()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if self.in_query.is_set():
            self.writer_embedded.set()
        else:
            pass
        return super().embed(texts)


def build_harness(
    blackboard: StoreBlackboard,
    clock: FakeClock,
    vectors: MemoryVectorIndex | ParkedQueries,
    model: MemoryModel,
) -> IndexerHarness:
    indexer = StoreIndexer(
        blackboard=blackboard,
        vectors=vectors,
        model=model,
        params=Params(),
        clock=clock,
        top_level=TOP_LEVEL,
    )
    blackboard.subscribe(indexer.apply)
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n"}, "spec")
    return IndexerHarness(indexer=indexer, blackboard=blackboard, clock=clock)


def approach_file(harness: IndexerHarness, approach_id: str) -> None:
    harness.blackboard.write(
        HUMAN, {f"approaches/{approach_id}.md": b"---\nstatus: active\n---\n"}, "a"
    )


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


@pytest.mark.parametrize(
    ("claim_type", "prelude", "actor", "to_status", "expected"),
    [
        ("result", ((EVAL, FAST),), WORKER, "quarantined", FAST),
        ("result", ((EVAL, FAST),), INTEGRATOR, "quarantined", "quarantined"),
        (
            "result",
            ((EVAL, FAST), (INTEGRATOR, "quarantined")),
            EVAL,
            FAST,
            "quarantined",
        ),
        ("result", ((EVAL, FAST), (INTEGRATOR, "quarantined")), HUMAN, FAST, FAST),
        ("metric", (), WORKER, "calibrated", "proposed"),
        ("metric", (), EVAL, "calibrated", "calibrated"),
        ("metric", ((EVAL, "calibrated"),), WORKER, "deprecated", "calibrated"),
        ("metric", ((EVAL, "calibrated"),), EVAL, "deprecated", "deprecated"),
        ("question", (), WORKER, "answered", "answered"),
    ],
)
def test_system_role_rows_of_the_d9_table(
    claim_type: str,
    prelude: tuple[tuple[ActorId, str], ...],
    actor: ActorId,
    to_status: str,
    expected: str,
) -> None:
    """Integrator quarantine, the human lift, eval's metric moves, and answering."""
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x"})
    claim = (
        harness.result_claim("aa01")
        if claim_type == "result"
        else harness.claim(OTHER, claim_type, "a note")
    )
    for prelude_actor, prelude_status in prelude:
        current = harness.indexer.claim_state(claim).status
        harness.status(prelude_actor, claim, prelude_status, current)
    current = harness.indexer.claim_state(claim).status
    harness.status(actor, claim, to_status, current)
    assert harness.indexer.claim_state(claim).status == expected


def test_only_the_planner_moves_an_approach_round_its_cycle() -> None:
    harness = make_indexer()
    approach_file(harness, "a-02")
    moves = [
        (WORKER, "frozen", "active"),
        (PLANNER, "frozen", "frozen"),
        (PLANNER, "dead_end", "dead_end"),
        (WORKER, "active", "dead_end"),
        (PLANNER, "active", "active"),
    ]
    seen: list[str] = []
    for actor, to_status, expected in moves:
        current = harness.indexer.approach_state("a-02").status
        harness.event(
            actor, "approach_status", target="a-02", **{"from": current}, to=to_status
        )
        seen.append(harness.indexer.approach_state("a-02").status)
    assert seen == [expected for _, _, expected in moves]
    assert len(harness.indexer.approach_state("a-02").history) == 3


def test_a_lower_priority_role_cannot_overturn_a_higher_one() -> None:
    """D34: human > Critic > eval service > ... ; the higher one wins."""
    harness = make_indexer()
    metric = harness.claim(WORKER, "metric", "latency p99")
    harness.status(CRITIC, metric, "calibrated", "proposed")
    harness.status(EVAL, metric, "deprecated", "calibrated")
    assert harness.indexer.claim_state(metric).status == "calibrated"
    harness.status(HUMAN, metric, "deprecated", "calibrated")
    assert harness.indexer.claim_state(metric).status == "deprecated"


def test_a_reinstate_or_lift_hands_the_claim_back_to_the_ladder() -> None:
    """A Critic's reinstate or a human's lift used to freeze the claim: the eval
    service could never verify it at the top level afterwards (D34)."""
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x"})
    result = harness.result_claim("aa01")
    harness.status(EVAL, result, FAST, "proposed")
    harness.status(CRITIC, result, "refuted", FAST)
    harness.event(CRITIC, "reinstated", target=result, reason="reproduced after all")
    harness.status(EVAL, result, VERIFIED, FAST)
    assert harness.indexer.claim_state(result).status == VERIFIED
    insight = harness.claim(WORKER, "insight", "a trick")
    harness.status(CRITIC, insight, "refuted", "proposed")
    harness.event(CRITIC, "reinstated", target=insight, reason="holds")
    harness.status(ActorId("indexer"), insight, "corroborated", "proposed")
    assert harness.indexer.claim_state(insight).status == "corroborated"
    harness.candidate(WORKER, "bb02", {"solver.py": "y"})
    lifted = harness.result_claim("bb02")
    harness.status(EVAL, lifted, FAST, "proposed")
    harness.status(INTEGRATOR, lifted, "quarantined", FAST)
    harness.status(HUMAN, lifted, FAST, "quarantined")
    harness.status(EVAL, lifted, VERIFIED, FAST)
    assert harness.indexer.claim_state(lifted).status == VERIFIED


def test_a_standing_refutation_blocks_a_lower_role_and_its_reinstate() -> None:
    """A Critic could reinstate what a human refuted; and after a human moved a
    refuted claim back to proposed, the old refuter still counted as a source."""
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "x"})
    result = harness.result_claim("aa01")
    dependent = harness.claim(WORKER, "insight", "builds on it", depends_on=(result,))
    harness.status(HUMAN, result, "refuted", "proposed")
    harness.event(CRITIC, "reinstated", target=result, reason="disagree")
    assert harness.indexer.claim_state(result).status == "refuted"
    harness.status(EVAL, result, FAST, "refuted")
    assert harness.indexer.claim_state(result).status == "refuted"
    harness.status(HUMAN, result, "proposed", "refuted")
    harness.status(EVAL, result, "refuted", "proposed")
    assert harness.indexer.claim_state(result).status == "refuted"
    assert not harness.indexer.claim_state(
        dependent
    ).is_cascaded_refuted, "one source after the human lift is still one"


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


def test_a_reinstated_dead_end_leaves_presearch_and_the_rewalk_metric() -> None:
    """A dead end stayed indexed after the Critic reinstated it, so a later
    top-level run on its payload counted as a re-walk, also after a rebuild."""
    harness = make_indexer()
    harness.candidate(WORKER, "aa01", {"solver.py": "unroll loops by four\n"})
    dead_end = harness.claim(
        WORKER,
        "dead_end",
        "unrolling did nothing",
        evidence=(EvidenceRef(candidate="candidates/aa01/candidate/solver.py"),),
    )
    harness.status(OTHER, dead_end, "confirmed", "proposed")
    payload = "solver.py\nunroll loops by four\n"
    assert [hit.item_id for hit in harness.indexer.similar_dead_ends(payload, 5, EVAL)]
    harness.status(CRITIC, dead_end, "reinstated", "confirmed")
    harness.candidate(WORKER, "bb02", {"solver.py": "unroll loops by four\n"})
    rerun = harness.result_claim("bb02")
    harness.event(
        EVAL,
        "result_ready",
        target=rerun,
        data={
            "candidate": "bb02",
            "claim_id": rerun,
            "approach": "a-01",
            "submitter": WORKER,
            "submitter_checkpoint": None,
            "baseline": "baseline@0",
            "paired": None,
            "per_item": "object://x",
            "score": 1.0,
            "level": TOP_LEVEL,
        },
    )
    assert harness.indexer.similar_dead_ends(payload, 5, EVAL) == ()
    assert harness.indexer.metrics().dead_end_rewalk_count == 0
    harness.indexer.rebuild()
    assert harness.indexer.similar_dead_ends(payload, 5, EVAL) == ()
    assert harness.indexer.metrics().dead_end_rewalk_count == 0


def test_a_search_never_reads_the_index_while_a_merge_writes_it() -> None:
    """Searching from a tool-server thread while a merge embedded crashed with
    "dictionary changed size during iteration"."""
    in_query = threading.Event()
    release = threading.Event()
    clock = FakeClock()
    vectors = ParkedQueries(
        inner=MemoryVectorIndex(), in_query=in_query, release=release
    )
    model = SignallingModel(in_query)
    blackboard = StoreBlackboard(
        store=MemoryVersionedStore(name="race", push_token=TOY_PUSH_TOKEN),
        push_token=TOY_PUSH_TOKEN,
        objects=None,
        mirror_store_factory=MemoryMirrorStores(),
        clock=clock,
    )
    harness = build_harness(blackboard, clock, vectors, model)
    harness.claim(WORKER, "insight", "caching halves the misses")
    hits: list[tuple[str, ...]] = []
    searcher = threading.Thread(
        target=lambda: hits.append(
            tuple(
                hit.item_id
                for hit in harness.indexer.search(QUERY, 5, OTHER, PRESETS["S2"])
            )
        )
    )
    writer = threading.Thread(
        target=lambda: harness.claim(WORKER, "insight", "caching idea two")
    )
    searcher.start()
    in_query.wait()
    writer.start()
    model.writer_embedded.wait()
    for _ in range(YIELD_COUNT):
        time.sleep(0)
    release.set()
    writer.join()
    searcher.join()
    assert not vectors.is_overlapped
    assert hits == [("c-worker-01-00001",)]
    assert len(harness.indexer.search(QUERY, 5, OTHER, PRESETS["S2"])) == 2


def test_a_rejected_corroboration_is_written_at_the_next_merge() -> None:
    """A rejected corroboration write was only logged; nothing retried it."""
    clock = FakeClock()
    blackboard = RejectingBlackboard(
        store=MemoryVersionedStore(name="rejecting", push_token=TOY_PUSH_TOKEN),
        push_token=TOY_PUSH_TOKEN,
        objects=None,
        mirror_store_factory=MemoryMirrorStores(),
        clock=clock,
    )
    harness = build_harness(blackboard, clock, MemoryVectorIndex(), MemoryModel())
    insight = harness.claim(OTHER, "insight", "cache-oblivious layouts help")
    results: list[str] = []
    for hash_value in ("aa01", "bb02"):
        harness.candidate(WORKER, hash_value, {"solver.py": hash_value})
        results.append(
            harness.claim(
                EVAL,
                "result",
                f"Result of {hash_value}.",
                evidence=(
                    EvidenceRef(candidate=f"candidates/{hash_value}/result.json"),
                ),
                depends_on=(insight,),
            )
        )
    for result in results:
        harness.status(EVAL, result, VERIFIED, "proposed")
    assert harness.indexer.claim_state(insight).status == "proposed", "rejected"
    harness.claim(THIRD, "question", "unrelated merge")
    harness.status(CRITIC, results[0], "refuted", VERIFIED)
    assert harness.indexer.claim_state(insight).status == "corroborated"
    assert len(harness.blackboard.list_paths("events/indexer/", None)) == 1


def test_corroboration_evidence_is_listed_by_claim_id_not_arrival() -> None:
    """Claims cite in merge order live and in path order on rebuild, so the same
    corroboration event carried its evidence in two orders."""
    harness = make_indexer()
    insight = harness.claim(WORKER, "insight", "caching halves the misses")
    later_id = harness.claim(THIRD, "question", "cites it", depends_on=(insight,))
    earlier_id = harness.claim(OTHER, "question", "cites it too", depends_on=(insight,))
    for hash_value, author in (("aa01", THIRD), ("bb02", OTHER)):
        harness.candidate(author, hash_value, {"solver.py": hash_value})
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
    for result in results:
        harness.status(EVAL, result, VERIFIED, "proposed")
    (event_path,) = harness.blackboard.list_paths("events/indexer/", None)
    event = json.loads(harness.blackboard.read(event_path, None))
    assert event["evidence"] == sorted([*results, earlier_id, later_id])
    assert harness.indexer.reuse_count(insight) == 4


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
