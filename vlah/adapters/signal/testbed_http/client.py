"""The testbed's slow tier as a SignalSource, its recorder, and its baseline scores
(Herd Testbed section 11.3, 11.4).

Every request carries X-TB-Run, so quota, queue and records stay per run. A
candidate is submitted as a zip of its payload plus a testbed manifest naming the
worker as the actor; the bytes are exactly what the worker ran tb fast on, so the
quota-discipline metric can match them by hash.
"""

import base64
import io
import json
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue

from vlah.core.blackboard.api import Blackboard
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import payload_files
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import PerItemScore, ScoreResult, SourceDecl
from vlah.ports.errors import JudgeBusyError
from vlah.ports.signal_source import CandidateRef, Dataset
from vlah.tools.api import RecorderEvent

RUN_HEADER = "X-TB-Run"
REQUEST_TIMEOUT_SECONDS = 30.0
BASELINE_ZERO = "baseline@0"


class SlowTierModel(BaseModel):
    """Replies from the testbed; unknown fields are ignored, so the testbed can add some."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class SlowTierAggregate(SlowTierModel):
    score: float | None
    ci: tuple[float, float] | None = None
    n: int
    per_seed: tuple[float, ...] = ()
    per_item: tuple[PerItemScore, ...] = ()
    extra: dict[str, JsonValue] = {}
    constraint_ok: bool | None = None


class SubmitReply(SlowTierModel):
    ticket: str
    duplicate: bool = False


class StatusReply(SlowTierModel):
    state: str
    result: SlowTierAggregate | None = None


class MetaReply(SlowTierModel):
    baseline: SlowTierAggregate


@dataclass(frozen=True, kw_only=True)
class HttpReply:
    status: int
    body: object


@dataclass(frozen=True, kw_only=True)
class SlowTierClient:
    base_url: str
    run_id: str

    def call(
        self, method: str, path: str, body: dict[str, JsonValue] | None
    ) -> HttpReply:
        if body is None:
            data = None
        else:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=data,
            method=method,
            headers={RUN_HEADER: self.run_id, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                reply_body: object = json.loads(response.read() or b"null")
                return HttpReply(status=response.status, body=reply_body)
        except urllib.error.HTTPError as error:
            error_body: object = json.loads(error.read() or b"null")
            return HttpReply(status=error.code, body=error_body)


def testbed_manifest(candidate: CandidateRef, actor: ActorId, baseline: str) -> bytes:
    """The manifest the testbed reads. The keys are confirmed with the testbed on #10."""
    manifest = candidate.manifest
    return yaml.safe_dump(
        {
            "actor": actor,
            "predicted_score": manifest.predicted_score,
            "parents": list(manifest.parents),
            "direction": manifest.direction or manifest.approach,
            "note": manifest.note or "",
            "target": baseline,
        },
        sort_keys=True,
    ).encode("utf-8")


def candidate_zip(candidate: CandidateRef, actor: ActorId, baseline: str) -> bytes:
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path, file_path in payload_files(candidate.payload_directory):
            archive.writestr(relative_path, file_path.read_bytes())
        archive.writestr("manifest.yaml", testbed_manifest(candidate, actor, baseline))
    return archive_bytes.getvalue()


def score_from_aggregate(aggregate: SlowTierAggregate) -> ScoreResult:
    return ScoreResult(
        value=aggregate.score,
        n=aggregate.n,
        ci=aggregate.ci,
        per_item=aggregate.per_item,
        per_seed=aggregate.per_seed,
        constraint_ok=aggregate.constraint_ok,
        extra=aggregate.extra,
    )


class SlowTierSource:
    """The slow tier as a pull judge; it reports 429s as JudgeBusyError and holds no quota."""

    def __init__(self, *, client: SlowTierClient, declaration: SourceDecl) -> None:
        self.client = client
        self.source_declaration = declaration

    @property
    def declaration(self) -> SourceDecl:
        return self.source_declaration

    def submit(
        self, candidate: CandidateRef, actor: ActorId, baseline: str, dataset: Dataset
    ) -> str:
        zip_text = base64.b64encode(candidate_zip(candidate, actor, baseline)).decode(
            "ascii"
        )
        reply = self.client.call(
            "POST", "/submit", {"zip_b64": zip_text, "dataset": dataset}
        )
        if reply.status == 200:
            return SubmitReply.model_validate(reply.body).ticket
        elif reply.status == 429 and isinstance(reply.body, dict):
            retry_at: object = reply.body.get("next_slot_at")
            if isinstance(retry_at, int | float):
                raise JudgeBusyError(float(retry_at))
            else:
                raise RuntimeError(
                    f"a 429 from the slow tier without next_slot_at: {reply.body}"
                )
        else:
            raise ValueError(
                f"the slow tier refused the candidate ({reply.status}): {reply.body}"
            )

    def poll(self, job_id: str) -> ScoreResult | None:
        reply = self.client.call("GET", f"/status/{job_id}", None)
        if reply.status != 200:
            raise RuntimeError(
                f"the slow tier status call failed ({reply.status}): {reply.body}"
            )
        else:
            status = StatusReply.model_validate(reply.body)
            if status.state == "done" and status.result is not None:
                return score_from_aggregate(status.result)
            elif status.state == "failed":
                return ScoreResult(value=None, n=1, constraint_ok=False)
            else:
                return None


class SlowTierRecorder:
    """Posts herd events (blackboard_write, blackboard_read, ...) to the run's recorder."""

    def __init__(self, *, client: SlowTierClient) -> None:
        self.client = client

    def post(self, events: tuple[RecorderEvent, ...]) -> None:
        reply = self.client.call(
            "POST",
            "/events",
            {"events": [event.model_dump(mode="json") for event in events]},
        )
        if reply.status != 200:
            raise RuntimeError(
                f"the recorder refused events ({reply.status}): {reply.body}"
            )
        else:
            pass


class SlowTierBaselineScores:
    """baseline@0 from the slow tier's /meta; later baselines from baseline/<k>.json."""

    def __init__(self, *, client: SlowTierClient, blackboard: Blackboard) -> None:
        self.client = client
        self.blackboard = blackboard
        self.baseline_zero: tuple[float, ...] | None = None

    def __call__(
        self, source_id: str, baseline: str, dataset: Dataset
    ) -> tuple[float, ...] | None:
        if baseline == BASELINE_ZERO:
            if self.baseline_zero is None:
                reply = self.client.call("GET", "/meta", None)
                self.baseline_zero = MetaReply.model_validate(
                    reply.body
                ).baseline.per_seed
            else:
                pass
            return self.baseline_zero
        else:
            record_path = f"baseline/{baseline.removeprefix('baseline@')}.json"
            if record_path not in self.blackboard.list_paths("baseline/", None):
                return None
            else:
                record = BaselineRecord.model_validate_json(
                    self.blackboard.read(record_path, None)
                )
                return record.per_seed or None
