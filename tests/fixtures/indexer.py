"""The real indexer on the in-memory blackboard, with helpers that write what it replays."""

import json
from dataclasses import dataclass, field

import yaml

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import MemoryVectorIndex
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId, role_of_actor
from vlah.core.types.params import Params

TOP_LEVEL = "slow"
APPROACH = "a-01"
HUMAN = ActorId("human")
EVAL = ActorId("eval_service")
CRITIC = ActorId("critic-1")


@dataclass(kw_only=True)
class IndexerHarness:
    indexer: StoreIndexer
    blackboard: StoreBlackboard
    clock: FakeClock
    event_numbers: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    claim_numbers: dict[ActorId, int] = field(default_factory=dict[ActorId, int])

    def claim(
        self,
        author: ActorId,
        claim_type: str,
        body: str,
        approach: str = APPROACH,
        evidence: tuple[EvidenceRef, ...] = (EvidenceRef(path="spec.md"),),
        depends_on: tuple[str, ...] = (),
    ) -> str:
        number = self.claim_numbers.get(author, 0) + 1
        self.claim_numbers[author] = number
        claim = Claim.model_validate(
            {
                "id": f"c-{author}-{number:05d}",
                "type": claim_type,
                "status": "open" if claim_type == "question" else "proposed",
                "approach": approach,
                "author": author,
                "evidence": [item.model_dump(exclude_none=True) for item in evidence],
                "depends_on": list(depends_on),
                "body": body,
            }
        )
        receipt = self.blackboard.write(
            author,
            {f"claims/{author}/{claim.id}.md": render_claim(claim).encode()},
            "c",
        )
        assert receipt.status != "rejected", receipt.violations
        return claim.id

    def event(self, actor: ActorId, event_type: str, **fields: object) -> None:
        number = self.event_numbers.get(actor, 0) + 1
        self.event_numbers[actor] = number
        local_id = f"{actor}/{number:05d}"
        body = {
            "local_id": local_id,
            "ts": self.clock.now_seconds(),
            "actor": actor,
            "role": role_of_actor(actor),
            "type": event_type,
            **fields,
        }
        receipt = self.blackboard.write(
            actor, {f"events/{local_id}.json": json.dumps(body).encode()}, event_type
        )
        assert receipt.status != "rejected", receipt.violations

    def status(self, actor: ActorId, claim_id: str, to: str, from_status: str) -> None:
        self.event(
            actor,
            "claim_status",
            target=claim_id,
            **{"from": from_status},
            to=to,
            reason="checked",
            evidence=["spec.md"],
        )

    def candidate(
        self, author: ActorId, hash_value: str, files: dict[str, str]
    ) -> None:
        manifest = {
            "kind": "patch",
            "target": "baseline@0",
            "approach": APPROACH,
            "author": author,
            "created_at": 0.0,
        }
        receipt = self.blackboard.write(
            author,
            {
                f"candidates/{hash_value}/manifest.yaml": yaml.safe_dump(
                    manifest
                ).encode(),
                **{
                    f"candidates/{hash_value}/candidate/{path}": text.encode()
                    for path, text in files.items()
                },
            },
            "candidate",
        )
        assert receipt.status != "rejected", receipt.violations

    def lineage(self, hash_value: str, parents: tuple[str, ...]) -> None:
        self.blackboard.write(
            EVAL,
            {
                f"candidates/{hash_value}/lineage.json": json.dumps(
                    {"parents": list(parents)}
                ).encode()
            },
            "lineage",
        )

    def result_claim(self, hash_value: str) -> str:
        return self.claim(
            EVAL,
            "result",
            f"Result of {hash_value}.",
            evidence=(EvidenceRef(candidate=f"candidates/{hash_value}/result.json"),),
        )


def make_indexer(params: Params | None = None) -> IndexerHarness:
    clock = FakeClock()
    blackboard = make_store_blackboard(clock, name="indexer")
    indexer = StoreIndexer(
        blackboard=blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=params or Params(),
        clock=clock,
        top_level=TOP_LEVEL,
    )
    blackboard.subscribe(indexer.apply)
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n"}, "spec")
    return IndexerHarness(indexer=indexer, blackboard=blackboard, clock=clock)
