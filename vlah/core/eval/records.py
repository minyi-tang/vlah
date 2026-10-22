"""What the eval client writes to the blackboard, and in which order (v2.2 D28, D54, D57).

The candidate is written as the submitting worker (or the Integrator, for a
remeasure); everything else is written as the eval service: result claims, gate
dead ends, lineage, result.json and events.
"""

import json
import logging
from pathlib import Path
from typing import Literal

from vlah.core.blackboard.api import Blackboard, WriteReceipt
from vlah.core.types.base import FrozenModel
from vlah.core.types.candidates import payload_files
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, EventPayload, EventType, event_to_json
from vlah.core.types.ids import ActorId, format_claim_id, format_event_local_id
from vlah.core.types.sources import Paired

logger = logging.getLogger(__name__)

EVAL_SERVICE = ActorId("eval_service")


class LevelResult(FrozenModel):
    """One level of result.json."""

    level: str
    score: float | None
    n: int
    per_seed: tuple[float, ...]
    paired: Paired
    predicted: float | None
    prediction_error: float | None
    constraint_ok: bool | None
    status: str


class ResultFile(FrozenModel):
    """candidates/<hash>/result.json, written once when the ladder ends."""

    candidate: str
    claim_id: str
    baseline: str
    levels: tuple[LevelResult, ...]


def require_merged(receipt: WriteReceipt, what: str) -> None:
    if receipt.status == "rejected":
        raise RuntimeError(f"the blackboard rejected {what}: {receipt.violations}")
    else:
        pass


class EvalRecords:
    """The eval client's writes; claim and event numbers continue after a restart."""

    def __init__(self, *, blackboard: Blackboard, clock: Clock) -> None:
        self.blackboard = blackboard
        self.clock = clock
        self.event_count = len(blackboard.list_paths(f"events/{EVAL_SERVICE}/", None))
        self.claim_count = len(blackboard.list_paths(f"claims/{EVAL_SERVICE}/", None))

    def write_candidate(
        self, writer: ActorId, candidate_directory: Path, hash_value: str
    ) -> None:
        """Write the candidate as its submitter; an identical copy already present is fine."""
        files = {
            f"candidates/{hash_value}/candidate/{relative_path}": file_path.read_bytes()
            for relative_path, file_path in payload_files(candidate_directory)
        }
        files[f"candidates/{hash_value}/manifest.yaml"] = (
            candidate_directory / "manifest.yaml"
        ).read_bytes()
        receipt = self.blackboard.write(writer, files, f"candidate {hash_value}")
        if receipt.status == "merged":
            return
        elif all(violation.rule == "file_exists" for violation in receipt.violations):
            # Without dedup (B1), a resubmitted candidate is already on the blackboard.
            return
        else:
            raise RuntimeError(
                f"the blackboard rejected candidate {hash_value}: {receipt.violations}"
            )

    def write_once(self, path: str, content: bytes, what: str) -> None:
        """Per-candidate files are written by the first request for that hash only.

        Without dedup (B1), a duplicate runs again; its record is its own result claim
        and events, and the candidate's lineage and first result stay as they were.
        """
        if path in self.blackboard.list_paths(path, None):
            logger.info(f"{path} already exists; keeping the first {what}")
        else:
            require_merged(
                self.blackboard.write(EVAL_SERVICE, {path: content}, what), what
            )

    def write_lineage(self, hash_value: str, parents: tuple[str, ...]) -> None:
        self.write_once(
            f"candidates/{hash_value}/lineage.json",
            json.dumps({"parents": list(parents)}).encode("utf-8"),
            f"lineage of {hash_value}",
        )

    def write_result(self, result_file: ResultFile) -> None:
        self.write_once(
            f"candidates/{result_file.candidate}/result.json",
            result_file.model_dump_json(indent=2).encode("utf-8"),
            f"result of {result_file.candidate}",
        )

    def write_claim(
        self,
        claim_type: Literal["result", "dead_end"],
        approach: str,
        evidence: EvidenceRef,
        body: str,
        tags: tuple[str, ...] = (),
    ) -> str:
        self.claim_count += 1
        claim = Claim(
            id=format_claim_id(EVAL_SERVICE, self.claim_count),
            type=claim_type,
            status="proposed",
            approach=approach,
            author=EVAL_SERVICE,
            tags=tags,
            evidence=(evidence,),
            body=body,
        )
        require_merged(
            self.blackboard.write(
                EVAL_SERVICE,
                {
                    f"claims/{EVAL_SERVICE}/{claim.id}.md": render_claim(claim).encode(
                        "utf-8"
                    )
                },
                f"{claim_type} claim {claim.id}",
            ),
            f"claim {claim.id}",
        )
        return claim.id

    def write_gate_dead_end(
        self, approach: str, hash_value: str, gate_name: str, output: str
    ) -> None:
        """A failed gate is never a score; it becomes a dead end others are warned about (D54)."""
        self.write_claim(
            "dead_end",
            approach,
            EvidenceRef(path=f"candidates/{hash_value}/manifest.yaml"),
            f"Gate {gate_name} failed for candidate {hash_value}.\n\n{output}",
            tags=(f"gate:{gate_name}",),
        )

    def write_event(
        self,
        event_type: EventType,
        *,
        target: str,
        data: EventPayload | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        source: str | None = None,
    ) -> Event:
        self.event_count += 1
        event = Event.model_validate(
            {
                "local_id": format_event_local_id(EVAL_SERVICE, self.event_count),
                "ts": self.clock.now_seconds(),
                "actor": EVAL_SERVICE,
                "role": "eval_service",
                "type": event_type,
                "target": target,
                "from": from_status,
                "to": to_status,
                "source": source,
                "data": None if data is None else data.model_dump(),
            }
        )
        require_merged(
            self.blackboard.write(
                EVAL_SERVICE,
                {f"events/{event.local_id}.json": event_to_json(event).encode("utf-8")},
                f"{event_type} {target}",
            ),
            f"event {event.local_id}",
        )
        return event
