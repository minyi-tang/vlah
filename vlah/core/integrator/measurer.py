"""The core's Measurer over the herd's EvalService (D74, C3).

The core asks for a measurement with submit and polls for it with result. submit
writes a manifest into the directory the core merged and calls
EvalService.remeasure, so the run is charged to the Integrator's quota, recorded as
a candidate written by the actor integrator, and answered with a remeasure_ready
event; the wrapper hands that event here as the measurement. The ticket is the
eval client's request id.

Audit measurements (dataset "audit") need RemeasureRequest.dataset (#95) and the
eval client passing it through to the judge. Until then submit reports them as
quota exhausted, which makes the core skip spot checks instead of failing.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vlah.core.eval.api import EvalService, RemeasureRequest
from vlah.core.integrator.core_adapter import Measurement, QuotaExhausted
from vlah.core.types.candidates import MANIFEST_FILE_NAME, CandidateManifest
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId

logger = logging.getLogger(__name__)

INTEGRATOR = ActorId("integrator")
DEV = "dev"
# The approach a measurement of a baseline itself (a revert, the deliverable) is
# recorded under: it belongs to no worker's approach.
BASELINE_APPROACH = "integrator"


@dataclass(frozen=True, kw_only=True)
class Submitted:
    ticket: str
    candidate_directory: Path
    baseline: int
    note: str


@dataclass(kw_only=True)
class EvalMeasurer:
    eval_service: EvalService
    clock: Clock
    # The approach of every candidate the core may remeasure, by its hash.
    approach_by_hash: dict[str, str] = field(default_factory=dict[str, str])
    results: dict[str, Measurement] = field(default_factory=dict[str, Measurement])
    submitted: list[Submitted] = field(default_factory=list[Submitted])

    def approach_of(self, parent: str | None) -> str:
        if parent is None:
            return BASELINE_APPROACH
        else:
            return self.approach_by_hash.get(parent, BASELINE_APPROACH)

    def write_manifest(
        self, candidate_directory: Path, baseline: int, parent: str | None, note: str
    ) -> CandidateManifest:
        """The manifest the eval client needs; merge and hash ignore it (TB 11.2)."""
        manifest = CandidateManifest(
            kind="patch",
            target=f"baseline@{baseline}",
            parents=() if parent is None else (parent,),
            approach=self.approach_of(parent),
            author=INTEGRATOR,
            created_at=self.clock.now_seconds(),
            note=note,
        )
        (candidate_directory / MANIFEST_FILE_NAME).write_text(
            yaml.safe_dump(manifest.model_dump(mode="json", exclude_none=True)),
            encoding="utf-8",
        )
        return manifest

    def submit(
        self,
        candidate_dir: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents: list[str],
        exempt: bool,
        predicted: float,
    ) -> str:
        """Queue one measurement through the eval client (the core's Measurer).

        exempt and predicted are the core's; the eval client charges every
        remeasure to the Integrator's quota and predicts from the parent itself.
        """
        if dataset != DEV:
            logger.warning(
                f"{dataset} measurements are not available through the eval client "
                "yet (#95); the audit quota counts as exhausted"
            )
            raise QuotaExhausted(None)
        else:
            parent = parents[0] if parents else None
            manifest = self.write_manifest(candidate_dir, baseline, parent, note)
            ticket = self.eval_service.remeasure(
                RemeasureRequest(
                    candidate_directory=candidate_dir,
                    baseline=manifest.target,
                    parent_candidate=manifest.target if parent is None else parent,
                    approach=manifest.approach,
                )
            )
            self.submitted.append(
                Submitted(
                    ticket=ticket,
                    candidate_directory=candidate_dir,
                    baseline=baseline,
                    note=note,
                )
            )
            return ticket

    def answer(self, measurement: Measurement) -> None:
        """A remeasure_ready event, as the measurement the core polls for."""
        self.results[measurement.ticket] = measurement

    def result(self, ticket: str) -> Measurement | None:
        return self.results.get(ticket)
