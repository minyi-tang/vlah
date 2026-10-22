"""SignalSource port: a judge that scores candidates (v2.2 D39; Herd Testbed section 11.3).

Quota, dedup and ordering are never an adapter's job; the eval client owns them.
"""

from pathlib import Path
from typing import Literal, Protocol

from vlah.core.types.base import FrozenModel
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import ScoreResult, SourceDecl

Dataset = Literal["dev", "audit", "judge"]


class CandidateRef(FrozenModel):
    candidate_hash: str
    payload_directory: Path
    manifest: CandidateManifest


class SignalSource(Protocol):
    @property
    def declaration(self) -> SourceDecl: ...

    def submit(
        self,
        candidate: CandidateRef,
        actor: ActorId,
        baseline: str,
        dataset: Dataset,
    ) -> str:
        """Queue a run and return its job id; actor is the worker, not the eval service."""
        ...

    def poll(self, job_id: str) -> ScoreResult | None:
        """The score once it is visible, else None.

        A judge that answered a repeat from its cache instead of running it says so
        with is_cached; a first submission of a candidate is always a run.
        """
        ...
