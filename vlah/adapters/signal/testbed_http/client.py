"""The testbed's slow tier as a SignalSource, its recorder, and its baseline scores
(Herd Testbed section 11.3, 11.4).

Every request carries X-TB-Run, so quota, queue and records stay per run. The
herd's eval client is a trusted service: it sends the testbed's system key
(X-TB-Key), which the workers' sandboxes cannot read, and names the actor it acts
for (X-TB-Actor). A candidate is submitted as a zip of its payload plus a testbed
manifest naming the worker as the actor; the bytes are exactly what the worker ran
tb fast on, so the quota-discipline metric can match them by hash.
"""

import base64
import io
import json
import math
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
KEY_HEADER = "X-TB-Key"
ACTOR_HEADER = "X-TB-Actor"
REQUEST_TIMEOUT_SECONDS = 30.0
BASELINE_ZERO = "baseline@0"


class SlowTierModel(BaseModel):
    """Replies from the testbed; unknown fields are ignored, so the testbed can add some."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class SlowTierAggregate(SlowTierModel):
    """A seed the candidate failed (crashed, timed out) has a null score (TB 11.3)."""

    score: float | None
    ci: tuple[float | None, float | None] | None = None
    n: int
    per_seed: tuple[float | None, ...] = ()
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
    system_key: str | None

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, JsonValue] | None,
        actor: ActorId | None = None,
    ) -> HttpReply:
        if body is None:
            data = None
        else:
            data = json.dumps(body).encode("utf-8")
        headers = {RUN_HEADER: self.run_id, "Content-Type": "application/json"}
        if self.system_key is not None:
            headers[KEY_HEADER] = self.system_key
        else:
            pass
        if actor is not None:
            headers[ACTOR_HEADER] = actor
        else:
            pass
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                reply_body: object = json.loads(response.read() or b"null")
                return HttpReply(status=response.status, body=reply_body)
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            try:
                error_body: object = json.loads(raw_body or b"null")
            except json.JSONDecodeError:
                error_body = raw_body.decode("utf-8", "replace")
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


def score_from_aggregate(aggregate: SlowTierAggregate, is_cached: bool) -> ScoreResult:
    """A run with a failed seed is void: no score, and its constraints are not met."""
    seeds = [seed for seed in aggregate.per_seed if seed is not None]
    if aggregate.score is None or len(seeds) != len(aggregate.per_seed):
        return ScoreResult(
            value=None,
            n=max(aggregate.n, 1),
            constraint_ok=False,
            extra=aggregate.extra,
            is_cached=is_cached,
        )
    else:
        low, high = aggregate.ci if aggregate.ci is not None else (None, None)
        return ScoreResult(
            value=aggregate.score,
            n=aggregate.n,
            ci=None if low is None or high is None else (low, high),
            per_item=aggregate.per_item,
            per_seed=tuple(seeds),
            constraint_ok=aggregate.constraint_ok,
            extra=aggregate.extra,
            is_cached=is_cached,
        )


class SlowTierSource:
    """The slow tier as a pull judge; it reports 429s as JudgeBusyError and holds no quota.

    A duplicate ticket (the testbed already had this hash) is a cache hit: its result
    says is_cached, so the eval client charges nothing for it.
    """

    def __init__(self, *, client: SlowTierClient, declaration: SourceDecl) -> None:
        self.client = client
        self.source_declaration = declaration
        self.actor_by_ticket: dict[str, ActorId] = {}
        self.cached_tickets: set[str] = set()

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
            submit_reply = SubmitReply.model_validate(reply.body)
            self.actor_by_ticket[submit_reply.ticket] = actor
            if submit_reply.duplicate:
                self.cached_tickets.add(submit_reply.ticket)
            else:
                pass
            return submit_reply.ticket
        elif reply.status == 429 and isinstance(reply.body, dict):
            retry_at: object = reply.body.get("next_slot_at")
            if isinstance(retry_at, int | float):
                raise JudgeBusyError(float(retry_at))
            else:
                # No next slot: this quota is used up for the rest of the run.
                raise JudgeBusyError(math.inf)
        elif reply.status >= 500:
            raise RuntimeError(
                f"the slow tier failed to take the candidate ({reply.status}): "
                f"{reply.body}"
            )
        else:
            raise ValueError(
                f"the slow tier refused the candidate ({reply.status}): {reply.body}"
            )

    def poll(self, job_id: str) -> ScoreResult | None:
        reply = self.client.call(
            "GET", f"/status/{job_id}", None, actor=self.actor_by_ticket.get(job_id)
        )
        if reply.status != 200:
            raise RuntimeError(
                f"the slow tier status call failed ({reply.status}): {reply.body}"
            )
        else:
            status = StatusReply.model_validate(reply.body)
            if status.state == "done" and status.result is not None:
                return score_from_aggregate(
                    status.result, job_id in self.cached_tickets
                )
            elif status.state == "failed":
                # The testbed's own infrastructure failed; the candidate is not judged.
                error = status.result.extra.get("error") if status.result else None
                raise ValueError(f"the slow tier could not run {job_id}: {error}")
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
                per_seed = MetaReply.model_validate(reply.body).baseline.per_seed
                seeds = tuple(seed for seed in per_seed if seed is not None)
                if len(seeds) != len(per_seed):
                    raise ValueError("the slow tier's baseline has a void seed")
                else:
                    self.baseline_zero = seeds
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
