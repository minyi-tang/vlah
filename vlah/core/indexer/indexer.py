"""The indexer: replays the blackboard into state and answers the sharing questions (D13, D18, D56).

apply folds one merged commit in, from the blackboard's merge delivery. rebuild
replays from sequence 0 using only the repository: each event at its number, and
each file at the first number whose snapshot shows it, so the two always agree
(L0-2). A claim that merged without an event is stamped with the next event's
number; it reaches other agents' diffs then (see DECISIONS, "Claims and sequence
numbers").

Search and similarity go through the VectorIndex and Model.embed ports. Two locks,
never nested: one guards the replayed state, because apply runs on the writing
thread and queries come from tool-server threads; the other serializes the vector
index, so a search never reads it while an upsert changes it. Model.embed runs
under neither, so a slow embedding blocks no diff_since.
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
)
from vlah.core.types.claims import Claim, parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.ports.model import Model
from vlah.ports.store import VectorHit, VectorIndex

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
        self.state = ReplayState(top_level=top_level)
        self.catalog = Catalog()
        self.corroboration_requested: set[str] = set()
        # Dead ends whose status changed since embed last ran.
        self.changed_dead_ends: set[str] = set()
        self.lock = threading.RLock()
        self.vector_lock = threading.Lock()
        # Every item id upserted so far, so a rebuild can empty the index.
        self.indexed_item_ids: set[str] = set()

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
        self.embed(to_embed, replace_everything=False)
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
            record = state.claims.get(event.target or "")
            if (
                record is not None
                and record.claim.type == "dead_end"
                and record.status != before
            ):
                self.changed_dead_ends.add(record.claim.id)
            else:
                pass
        return to_embed

    def claim_status_of(self, claim_id: str | None) -> str | None:
        record = self.state.claims.get(claim_id or "")
        return record.status if record is not None else None

    # Embedding

    def embed(self, items: list[ToEmbed], replace_everything: bool) -> None:
        """Embed under no lock, then change the vector index under its own lock."""
        with self.lock:
            everything = [*items, *self.catalog.take_changed_candidates()]
            deletes: list[str] = []
            # Only a confirmed dead end is presearched (D26). Its latest status
            # decides, so one that was confirmed and then reinstated is dropped.
            for claim_id in sorted(self.changed_dead_ends):
                record = self.state.claims[claim_id]
                if record.status == "confirmed":
                    everything.append(
                        self.catalog.dead_end_item(
                            record.claim, candidate_hashes_of(record.claim)
                        )
                    )
                else:
                    deletes.append(f"{DEAD_END_PREFIX}{claim_id}")
            self.changed_dead_ends.clear()
        if everything:
            embedded = self.model.embed(tuple(item.text for item in everything))
        else:
            embedded = ()
        with self.vector_lock:
            if replace_everything:
                deletes = sorted(self.indexed_item_ids)
            else:
                pass
            for item_id in deletes:
                self.vectors.delete(item_id)
                self.indexed_item_ids.discard(item_id)
            for item, vector in zip(everything, embedded, strict=True):
                self.vectors.upsert(item.item_id, vector, item.metadata)
                self.indexed_item_ids.add(item.item_id)

    # Corroboration (D9): an insight cited by two results verified at the top level

    def request_corroborations(self) -> None:
        """Write the corroborations due, by insight id; a rejected one is due again."""
        with self.lock:
            due = [
                claim_id
                for claim_id in sorted(self.state.claims)
                if (record := self.state.claims[claim_id]).claim.type == "insight"
                and record.status == "proposed"
                and claim_id not in self.corroboration_requested
                and corroborating_results(self.state, claim_id, self.top_level)
                >= CORROBORATING_RESULTS
            ]
            self.corroboration_requested.update(due)
            evidence = {insight: self.state.citations_of(insight) for insight in due}
        for insight_id in due:
            is_written = write_corroboration(
                self.blackboard, self.clock, insight_id, evidence[insight_id]
            )
            if is_written:
                pass
            else:
                with self.lock:
                    self.corroboration_requested.discard(insight_id)

    # Rebuild

    def rebuild(self) -> None:
        """Replay from sequence 0 using only the repository."""
        replayed = ReplayState(top_level=self.top_level)
        to_embed: list[ToEmbed] = []
        with self.lock:
            self.state = replayed
            self.catalog = Catalog()
            self.changed_dead_ends.clear()
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
        self.embed(to_embed, replace_everything=True)
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

    def head_sequence(self) -> int:
        with self.lock:
            return self.state.head

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

    def vector_hits(
        self, text: str, limit: int, where: dict[str, str] | None
    ) -> tuple[VectorHit, ...]:
        (vector,) = self.model.embed((text,))
        with self.vector_lock:
            return self.vectors.query(vector, limit, where)

    def query(
        self, text: str, limit: int, where: dict[str, str] | None
    ) -> tuple[Hit, ...]:
        hits = self.vector_hits(text, limit, where)
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
            hits = self.vector_hits(query, limit * 2, None)
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
            measured = sorted(self.state.top_level_measured)
            texts = {
                candidate: self.catalog.candidate_text(candidate)
                for candidate in measured
            }
            reuse = {
                claim_id: len(self.state.citations_of(claim_id))
                for claim_id in sorted(self.state.claims)
                if self.state.claims[claim_id].claim.type == "insight"
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
