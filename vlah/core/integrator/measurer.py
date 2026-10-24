"""The core's Measurer over the herd's EvalService (D74, C3).

The core asks for a measurement with submit and polls for it with result. submit
writes a manifest into the directory the core merged and calls
EvalService.remeasure, so the run is charged to the Integrator's quota, recorded as
a candidate written by the actor integrator, and answered with a remeasure_ready
event; the wrapper hands that event here as the measurement. The ticket is the
eval client's request id. A spot check's audit measurement is the same request
on the audit dataset (D72); the eval client counts it under its own cap.

A void answer (the judge could not score the run) is never handed to the core,
which would read its empty scores as a measurement: it is submitted again, up to
VOID_RETRIES times, and the wrapper decides what happens after that.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from vlah.core.eval.api import EvalService, RemeasureRequest
from vlah.core.integrator.core_adapter import Measurement
from vlah.core.types.candidates import MANIFEST_FILE_NAME, CandidateManifest
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId

logger = logging.getLogger(__name__)

INTEGRATOR = ActorId("integrator")
MeasurementDataset = Literal["dev", "audit"]
# The approach a measurement of a baseline itself (a revert, the deliverable) is
# recorded under: it belongs to no worker's approach.
BASELINE_APPROACH = "integrator"
VOID_RETRIES = 2


@dataclass(frozen=True, kw_only=True)
class Submitted:
    ticket: str
    candidate_directory: Path
    baseline: int
    dataset: MeasurementDataset
    note: str
    request: RemeasureRequest


@dataclass(kw_only=True)
class EvalMeasurer:
    eval_service: EvalService
    clock: Clock
    # The approach of every candidate the core may remeasure, by its hash.
    approach_by_hash: dict[str, str] = field(default_factory=dict[str, str])
    results: dict[str, Measurement] = field(default_factory=dict[str, Measurement])
    submitted: list[Submitted] = field(default_factory=list[Submitted])
    # A resubmission's ticket, to the ticket the core is waiting on.
    original_by_ticket: dict[str, str] = field(default_factory=dict[str, str])
    retries_by_ticket: dict[str, int] = field(default_factory=dict[str, int])

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
        remeasure to the Integrator's quota, audit measurements to their own
        cap, and predicts from the parent itself.
        """
        if dataset == "dev":
            requested: MeasurementDataset = "dev"
        elif dataset == "audit":
            requested = "audit"
        else:
            raise ValueError(f"the core asked for an unknown dataset {dataset!r}")
        parent = parents[0] if parents else None
        manifest = self.write_manifest(candidate_dir, baseline, parent, note)
        request = RemeasureRequest(
            candidate_directory=candidate_dir,
            baseline=manifest.target,
            parent_candidate=manifest.target if parent is None else parent,
            approach=manifest.approach,
            dataset=requested,
        )
        ticket = self.eval_service.remeasure(request)
        self.submitted.append(
            Submitted(
                ticket=ticket,
                candidate_directory=candidate_dir,
                baseline=baseline,
                dataset=requested,
                note=note,
                request=request,
            )
        )
        return ticket

    def original_ticket(self, ticket: str) -> str:
        return self.original_by_ticket.get(ticket, ticket)

    def submission(self, ticket: str) -> Submitted | None:
        original = self.original_ticket(ticket)
        return next((item for item in self.submitted if item.ticket == original), None)

    def resubmit(self, ticket: str) -> bool:
        """Ask again after a void answer; False once VOID_RETRIES are spent."""
        original = self.original_ticket(ticket)
        submitted = self.submission(original)
        retries = self.retries_by_ticket.get(original, 0)
        if submitted is None or retries >= VOID_RETRIES:
            return False
        else:
            again = self.eval_service.remeasure(submitted.request)
            self.original_by_ticket[again] = original
            self.retries_by_ticket[original] = retries + 1
            logger.warning(
                f"the judge returned no score for {submitted.note}; asking again "
                f"({retries + 1} of {VOID_RETRIES})"
            )
            return True

    def answer(self, measurement: Measurement) -> None:
        """A remeasure_ready event, as the measurement the core polls for."""
        original = self.original_ticket(measurement.ticket)
        self.results[original] = Measurement(
            original,
            measurement.hash,
            measurement.per_seed,
            measurement.score,
            measurement.constraint_ok,
        )

    def result(self, ticket: str) -> Measurement | None:
        return self.results.get(ticket)
