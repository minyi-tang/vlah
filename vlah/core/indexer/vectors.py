"""What the indexer embeds for search and presearch, and how (v2.2 section 7, D26, D69).

Indexed items, each with metadata the queries filter on:
- every claim: its type, approach and body (kind claim);
- every candidate: its payload text, built from candidates/<hash>/candidate/* in
  the same form as candidates.payload_text, so a new candidate's text compares
  like for like, and the target baseline is never part of it (kind candidate);
- every confirmed dead end: the payload it tested, or its own words when it cites
  no candidate (kind dead_end);
- digests and the charter (kinds digest and charter).
The catalog is touched only with the indexer's lock held; embed and query run the
model and the vector index without it.
"""

from dataclasses import dataclass, field

from vlah.core.indexer.api import HitKind
from vlah.core.types.claims import Claim
from vlah.ports.model import Model
from vlah.ports.store import VectorHit, VectorIndex

CANDIDATE_PAYLOAD = "candidate/"
CANDIDATE_ITEM_PREFIX = "candidate/"
DEAD_END_PREFIX = "dead-end/"
INDEXED_DOCUMENTS: tuple[tuple[str, HitKind], ...] = (
    ("digests/", "digest"),
    ("charter/", "charter"),
)
HIT_KINDS: dict[str, HitKind] = {
    "claim": "claim",
    "dead_end": "claim",
    "candidate": "candidate",
    "digest": "digest",
    "charter": "charter",
}


@dataclass(frozen=True, kw_only=True)
class ToEmbed:
    item_id: str
    text: str
    metadata: dict[str, str]


def claim_item(claim: Claim) -> ToEmbed:
    return ToEmbed(
        item_id=claim.id,
        text=f"{claim.type} {claim.approach}: {claim.body}",
        metadata={
            "kind": "claim",
            "type": claim.type,
            "approach": claim.approach,
            "author": claim.author,
        },
    )


@dataclass(kw_only=True)
class Catalog:
    """Candidate payloads by file, and which changed since they were last embedded.

    Files are kept by path and rendered in sorted order, so a commit delivered twice
    changes nothing and the text matches candidates.payload_text whatever order the
    files arrived in.
    """

    payloads: dict[str, dict[str, str]] = field(
        default_factory=dict[str, dict[str, str]]
    )
    changed_candidates: set[str] = field(default_factory=set[str])

    def candidate_text(self, candidate: str) -> str:
        files = self.payloads.get(candidate, {})
        return "\n".join(
            f"{relative}\n{text}" for relative, text in sorted(files.items())
        )

    def document_items(self, path: str, content: bytes) -> list[ToEmbed]:
        """A digest or charter file to embed; a candidate file joins its payload."""
        text = content.decode("utf-8", "replace")
        segments = path.split("/")
        if (
            len(segments) >= 4
            and segments[0] == "candidates"
            and segments[2] == "candidate"
        ):
            candidate = segments[1]
            relative = path.split(f"candidates/{candidate}/{CANDIDATE_PAYLOAD}", 1)[1]
            files = self.payloads.setdefault(candidate, {})
            if files.get(relative) != text:
                files[relative] = text
                self.changed_candidates.add(candidate)
            else:
                pass
            return []
        else:
            return [
                ToEmbed(item_id=path, text=text, metadata={"kind": kind})
                for prefix, kind in INDEXED_DOCUMENTS
                if path.startswith(prefix)
            ]

    def dead_end_item(self, claim: Claim, candidates: tuple[str, ...]) -> ToEmbed:
        """Compared with a new candidate's payload, so the payload it tested is the
        like-for-like text."""
        cited = [
            text for candidate in candidates if (text := self.candidate_text(candidate))
        ]
        return ToEmbed(
            item_id=f"{DEAD_END_PREFIX}{claim.id}",
            text="\n".join(cited) if cited else claim.body,
            metadata={"kind": "dead_end", "approach": claim.approach},
        )

    def take_changed_candidates(self) -> list[ToEmbed]:
        changed = sorted(self.changed_candidates)
        self.changed_candidates.clear()
        return [
            ToEmbed(
                item_id=f"{CANDIDATE_ITEM_PREFIX}{candidate}",
                text=self.candidate_text(candidate),
                metadata={"kind": "candidate"},
            )
            for candidate in changed
        ]


def embed(model: Model, vectors: VectorIndex, items: list[ToEmbed]) -> None:
    if not items:
        return
    else:
        embedded = model.embed(tuple(item.text for item in items))
        for item, vector in zip(items, embedded, strict=True):
            vectors.upsert(item.item_id, vector, item.metadata)


def query(
    model: Model,
    vectors: VectorIndex,
    text: str,
    limit: int,
    where: dict[str, str] | None,
) -> tuple[VectorHit, ...]:
    (vector,) = model.embed((text,))
    return vectors.query(vector, limit, where)
