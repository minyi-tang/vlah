"""The indexer: replays the blackboard into state and answers the sharing questions (D13, D18, D56).

apply folds one merged commit in, from the blackboard's merge delivery. rebuild
replays from sequence 0 using only the repository: each event at its number, and
each file at the first number whose snapshot shows it, so the two always agree
(L0-2). A claim that merged without an event is stamped with the next event's
number; it reaches other agents' diffs then (see DECISIONS, "Claims and sequence
numbers").

Search and similarity go through the VectorIndex and Model.embed ports. Embedding
runs outside the lock; one lock guards the state, because apply runs on the writing
thread and queries come from tool-server threads.
"""

import logging
import threading
from collections.abc import Iterable

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard, MergedCommit
from vlah.core.indexer.api import (
    ApproachState,
    ClaimState,
    Diff,
    Hit,
    IndexerMetrics,
    WorkingSet,
)
from vlah.core.indexer.corroboration import (
    CORROBORATING_RESULTS,
    corroborating_results,
    write_corroboration,
)
from vlah.core.indexer.diff import build_diff
from vlah.core.indexer.state import UNKNOWN_STATUS, ReplayState, candidate_hashes_of
from vlah.core.indexer.vectors import (
    CANDIDATE_ITEM_PREFIX,
    DEAD_END_PREFIX,
    HIT_KINDS,
    Catalog,
    ToEmbed,
    claim_item,
    embed,
)
from vlah.core.indexer.vectors import query as query_vectors
from vlah.core.types.claims import Claim, parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, ResultReady, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.ports.model import Model
from vlah.ports.store import VectorIndex

logger = logging.getLogger(__name__)

INDEXER = ActorId("indexer")
SEQUENCE_POINTER = "events/_seq/{}"
SUMMARY_CHARACTERS = 160


def is_claim_path(path: str) -> bool:
    return path.startswith("claims/") and path.endswith(".md")


class StoreIndexer:
    def __init__(
        self,
        *,
        blackboard: Blackboard,
        vectors: VectorIndex,
        model: Model,
        params: Params,
        clock: Clock,
        top_level: str | None,
    ) -> None:
        self.blackboard = blackboard
        self.vectors = vectors
        self.model = model
        self.params = params
        self.clock = clock
        self.top_level = top_level
        self.state = ReplayState()
        self.catalog = Catalog()
        self.top_level_measured: set[str] = set()
        self.corroboration_requested: set[str] = set()
        self.lock = threading.RLock()

    # Folding in merges

    def apply(self, merged_commit: MergedCommit) -> None:
        """Fold one merge into state; the same sequence numbers twice is a no-op."""
        events = sorted(
            (event for event in merged_commit.events if event.seq is not None),
            key=lambda event: event.seq or 0,
        )
        first_sequence = events[0].seq if events else None
        with self.lock:
            if events and all(
                event.seq in self.state.applied_sequences for event in events
            ):
                return
            else:
                contents = {
                    path: self.blackboard.read(path, None)
                    for path in merged_commit.paths
                    if not is_claim_path(path) and not path.startswith("events/")
                }
                to_embed = self.fold(
                    merged_commit.claims, contents, events, first_sequence
                )
        self.embed(to_embed)
        self.request_corroborations()

    def fold(
        self,
        claims: Iterable[Claim],
        contents: dict[str, bytes],
        events: list[Event],
        first_sequence: int | None,
    ) -> list[ToEmbed]:
        """With the lock held: files first, then the commit's events in order."""
        state = self.state
        to_embed: list[ToEmbed] = []
        for claim in claims:
            state.add_claim(claim, None)
            to_embed.append(claim_item(claim))
        for path, content in sorted(contents.items()):
            state.add_file(path, content)
            to_embed.extend(self.catalog.document_items(path, content))
        if first_sequence is not None:
            state.stamp_pending(first_sequence)
        else:
            pass
        for event in events:
            before = self.claim_status_of(event.target)
            state.apply_event(event)
            self.note_measurement(event)
            to_embed.extend(self.dead_end_items(event.target, before))
        return to_embed

    def claim_status_of(self, claim_id: str | None) -> str | None:
        record = self.state.claims.get(claim_id or "")
        return record.status if record is not None else None

    def note_measurement(self, event: Event) -> None:
        """Candidates judged at the top level count for the dead-end re-walk metric."""
        if isinstance(event.data, ResultReady) and event.data.level == self.top_level:
            self.top_level_measured.add(event.data.candidate)
        else:
            pass

    # Embedding

    def dead_end_items(self, claim_id: str | None, before: str | None) -> list[ToEmbed]:
        """A dead end newly confirmed joins the presearch index (D26)."""
        record = self.state.claims.get(claim_id or "")
        if (
            record is None
            or record.claim.type != "dead_end"
            or record.status != "confirmed"
            or before == "confirmed"
        ):
            return []
        else:
            return [
                self.catalog.dead_end_item(
                    record.claim, candidate_hashes_of(record.claim)
                )
            ]

    def embed(self, items: list[ToEmbed]) -> None:
        """Embed outside the lock: the model may be slow."""
        with self.lock:
            everything = [*items, *self.catalog.take_changed_candidates()]
        embed(self.model, self.vectors, everything)

    # Corroboration (D9): an insight cited by two results verified at the top level

    def request_corroborations(self) -> None:
        with self.lock:
            due = [
                record.claim.id
                for record in self.state.claims.values()
                if record.claim.type == "insight"
                and record.status == "proposed"
                and record.claim.id not in self.corroboration_requested
                and corroborating_results(self.state, record.claim.id, self.top_level)
                >= CORROBORATING_RESULTS
            ]
            self.corroboration_requested.update(due)
            evidence = {insight: self.state.citations_of(insight) for insight in due}
        for insight_id in due:
            write_corroboration(
                self.blackboard, self.clock, insight_id, evidence[insight_id]
            )

    # Rebuild

    def rebuild(self) -> None:
        """Replay from sequence 0 using only the repository."""
        replayed = ReplayState()
        to_embed: list[ToEmbed] = []
        with self.lock:
            self.state = replayed
            self.catalog = Catalog()
            self.top_level_measured = set()
            seen: set[str] = set()
            head = self.blackboard.head_sequence()
            for sequence in range(1, head + 1):
                to_embed.extend(
                    self.replay_files(
                        self.blackboard.list_paths("", sequence), seen, sequence
                    )
                )
                to_embed.extend(self.fold((), {}, [self.event_at(sequence)], sequence))
            to_embed.extend(
                self.replay_files(self.blackboard.list_paths("", None), seen, None)
            )
            self.corroboration_requested = {
                claim_id
                for claim_id, record in replayed.claims.items()
                if record.status != "proposed" and record.claim.type == "insight"
            }
        self.embed(to_embed)
        self.request_corroborations()

    def replay_files(
        self, paths: tuple[str, ...], seen: set[str], sequence: int | None
    ) -> list[ToEmbed]:
        new_paths = [
            path
            for path in paths
            if path not in seen and not path.startswith("events/")
        ]
        seen.update(new_paths)
        claims = [
            parse_claim(self.blackboard.read(path, sequence).decode("utf-8"))
            for path in new_paths
            if is_claim_path(path)
        ]
        contents = {
            path: self.blackboard.read(path, sequence)
            for path in new_paths
            if not is_claim_path(path)
        }
        to_embed: list[ToEmbed] = []
        for claim in claims:
            self.state.add_claim(claim, sequence)
            to_embed.append(claim_item(claim))
        for path, content in sorted(contents.items()):
            self.state.add_file(path, content)
            to_embed.extend(self.catalog.document_items(path, content))
        return to_embed

    def event_at(self, sequence: int) -> Event:
        path = self.blackboard.read(
            SEQUENCE_POINTER.format(sequence), sequence
        ).decode()
        event = event_from_json(self.blackboard.read(path, sequence).decode())
        return event.model_copy(update={"seq": sequence})

    def fingerprint(self) -> str:
        with self.lock:
            return self.state.fingerprint()

    # Queries

    def claim_state(self, claim_id: str) -> ClaimState:
        with self.lock:
            return self.state.claim_state(claim_id)

    def approach_state(self, approach_id: str) -> ApproachState:
        with self.lock:
            record = self.state.approaches.get(approach_id)
            if record is None:
                return ApproachState(
                    approach_id=approach_id, status=UNKNOWN_STATUS, history=()
                )
            else:
                return ApproachState(
                    approach_id=approach_id,
                    status=record.status,
                    history=tuple(record.history),
                )

    def query(
        self, text: str, limit: int, where: dict[str, str] | None
    ) -> tuple[Hit, ...]:
        hits = query_vectors(self.model, self.vectors, text, limit, where)
        with self.lock:
            return tuple(
                self.hit_of(hit.item_id, hit.similarity, hit.metadata) for hit in hits
            )

    def hit_of(self, item_id: str, similarity: float, metadata: dict[str, str]) -> Hit:
        """With the lock held: a vector hit as the contract's Hit."""
        kind = HIT_KINDS.get(metadata.get("kind", "claim"), "claim")
        record = self.state.claims.get(item_id.removeprefix(DEAD_END_PREFIX))
        candidate = item_id.removeprefix(CANDIDATE_ITEM_PREFIX)
        if record is not None:
            identifier, summary = record.claim.id, record.claim.body
        elif kind == "candidate":
            identifier, summary = candidate, self.catalog.candidate_text(candidate)
        else:
            identifier, summary = item_id, ""
        return Hit(
            item_id=identifier,
            kind=kind,
            similarity=similarity,
            summary=summary[:SUMMARY_CHARACTERS],
        )

    def search(
        self, query: str, limit: int, viewer: ActorId, arm: ArmConfig
    ) -> tuple[Hit, ...]:
        """Scoped by arm.search: on, candidates_only, own_notes, or off (B1: nothing)."""
        if arm.search == "off":
            return ()
        elif arm.search == "candidates_only":
            return self.query(query, limit, {"kind": "candidate"})
        elif arm.search == "own_notes":
            return self.query(query, limit, {"kind": "claim", "author": viewer})
        else:
            # A confirmed dead end is indexed twice, as a claim and for presearch.
            hits = query_vectors(self.model, self.vectors, query, limit * 2, None)
            kept = [hit for hit in hits if hit.metadata.get("kind") != "dead_end"]
            with self.lock:
                return tuple(
                    self.hit_of(hit.item_id, hit.similarity, hit.metadata)
                    for hit in kept[:limit]
                )

    def similar_candidates(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        """Compares payloads only, never targets (D69)."""
        return self.query(payload_text, limit, {"kind": "candidate"})

    def similar_dead_ends(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        """Confirmed dead ends only (D26)."""
        return self.query(payload_text, limit, {"kind": "dead_end"})

    def diff_since(
        self,
        cursor: int,
        working_set: WorkingSet,
        viewer: ActorId,
        max_tokens: int,
        arm: ArmConfig,
    ) -> Diff:
        with self.lock:
            head = self.state.head
            if arm.diff_since in ("off", "none"):
                return Diff(
                    items=(), cursor=max(cursor, head), omitted_count=0, rendered=""
                )
            else:
                return build_diff(
                    tuple(self.state.claims.values()),
                    cursor,
                    head,
                    working_set,
                    viewer,
                    max_tokens,
                    results_only=arm.diff_since == "results_only",
                )

    def reuse_count(self, claim_id: str) -> int:
        """How often a claim is built on: depends_on and claim evidence, each citer once."""
        with self.lock:
            return len(self.state.citations_of(claim_id))

    def metrics(self) -> IndexerMetrics:
        with self.lock:
            measured = sorted(self.top_level_measured)
            texts = {
                candidate: self.catalog.candidate_text(candidate)
                for candidate in measured
            }
            insights = [
                record.claim.id
                for record in self.state.claims.values()
                if record.claim.type == "insight"
            ]
            reuse = {
                insight: len(self.state.citations_of(insight)) for insight in insights
            }
        threshold = self.params.similarity_threshold
        rewalks = sum(
            1
            for candidate, text in texts.items()
            if text
            and any(
                hit.similarity >= threshold
                for hit in self.similar_dead_ends(text, 5, INDEXER)
                if candidate not in self.dead_end_candidates(hit.item_id)
            )
        )
        return IndexerMetrics(dead_end_rewalk_count=rewalks, insight_reuse_counts=reuse)

    def dead_end_candidates(self, claim_id: str) -> tuple[str, ...]:
        with self.lock:
            record = self.state.claims.get(claim_id)
            return candidate_hashes_of(record.claim) if record is not None else ()
