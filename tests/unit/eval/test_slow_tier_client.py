"""The testbed slow-tier adapter against a local fake of its HTTP API (TB section 11.3)."""

import base64
import io
import json
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.signal.testbed_http.client import (
    RUN_HEADER,
    SlowTierBaselineScores,
    SlowTierClient,
    SlowTierRecorder,
    SlowTierSource,
)
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import SourceBudget, SourceDecl
from vlah.ports.errors import JudgeBusyError
from vlah.ports.signal_source import CandidateRef
from vlah.testing.conformance import check_signal_source
from vlah.tools.api import RecorderEvent

pytestmark = pytest.mark.eval

WORKER = ActorId("worker-07")
DECLARATION = SourceDecl(
    id="slow",
    mode="pull",
    latency="10m",
    budget=SourceBudget(per_day=48, allocated_by="human"),
    cost_machine_hours=1.0,
    trust=0.9,
    readable_by=("critic",),
)
BASELINE_PER_SEED = [-120.0, -118.0, -125.0]


@dataclass(kw_only=True)
class FakeSlowTierState:
    clock: FakeClock
    submissions_left: int = 100
    next_slot_at: float | None = 3600.0
    tickets: dict[str, float] = field(default_factory=dict[str, float])
    ticket_by_archive: dict[bytes, str] = field(default_factory=dict[bytes, str])
    archives: list[bytes] = field(default_factory=list[bytes])
    run_headers: list[str] = field(default_factory=list[str])
    key_headers: list[str] = field(default_factory=list[str])
    status_actors: list[str] = field(default_factory=list[str])
    events: list[object] = field(default_factory=list[object])
    status_override: dict[str, object] | None = None
    submit_override: tuple[int, bytes] | None = None


def handler_for(state: FakeSlowTierState) -> type[BaseHTTPRequestHandler]:
    class SlowTierHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            pass

        def reply(self, status: int, body: object) -> None:
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            state.run_headers.append(self.headers.get(RUN_HEADER, ""))
            state.key_headers.append(self.headers.get("X-TB-Key", ""))
            if self.path == "/meta":
                self.reply(
                    200,
                    {
                        "baseline": {
                            "score": -121.0,
                            "n": 3,
                            "per_seed": BASELINE_PER_SEED,
                        }
                    },
                )
            elif self.path.startswith("/status/"):
                state.status_actors.append(self.headers.get("X-TB-Actor", ""))
                ticket = self.path.removeprefix("/status/")
                if state.status_override is not None:
                    self.reply(200, state.status_override)
                elif state.clock.now_seconds() >= state.tickets[ticket]:
                    self.reply(
                        200,
                        {
                            "state": "done",
                            "result": {
                                "score": -110.0,
                                "n": 3,
                                "per_seed": [-110.0, -108.0, -112.0],
                                "constraint_ok": True,
                                "ingredients": ["backfill"],
                            },
                        },
                    )
                else:
                    self.reply(200, {"state": "queued"})
            else:
                self.reply(404, {"reason": "not_found"})

        def do_POST(self) -> None:
            state.run_headers.append(self.headers.get(RUN_HEADER, ""))
            body: object = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            assert isinstance(body, dict)
            if self.path == "/submit":
                if state.submit_override is not None:
                    status, raw = state.submit_override
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                elif base64.b64decode(str(body["zip_b64"])) in state.ticket_by_archive:
                    # The slow tier keeps one ticket per hash and charges no quota (11.3).
                    archive = base64.b64decode(str(body["zip_b64"]))
                    original = state.ticket_by_archive[archive]
                    ticket = f"ticket-{len(state.tickets) + 1}"
                    state.tickets[ticket] = state.tickets[original]
                    self.reply(
                        200, {"ticket": ticket, "duplicate": True, "of": original}
                    )
                elif state.submissions_left <= 0:
                    self.reply(
                        429, {"reason": "quota", "next_slot_at": state.next_slot_at}
                    )
                else:
                    state.submissions_left -= 1
                    archive = base64.b64decode(str(body["zip_b64"]))
                    state.archives.append(archive)
                    ticket = f"ticket-{len(state.tickets) + 1}"
                    state.tickets[ticket] = state.clock.now_seconds() + 600.0
                    state.ticket_by_archive[archive] = ticket
                    self.reply(
                        200, {"ticket": ticket, "position": 1, "visible_at": 600.0}
                    )
            elif self.path == "/events":
                state.events.extend(body["events"])
                self.reply(200, {"ok": True})
            else:
                self.reply(404, {"reason": "not_found"})

    return SlowTierHandler


@pytest.fixture
def slow_tier() -> Iterator[tuple[SlowTierClient, FakeSlowTierState]]:
    state = FakeSlowTierState(clock=FakeClock())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    yield SlowTierClient(
        base_url=f"http://{host!s}:{port}", run_id="r-test", system_key="system-key"
    ), state
    server.shutdown()
    server.server_close()


def candidate(directory: Path) -> CandidateRef:
    (directory / "policy.py").write_bytes(
        b"def order(queue, state):\n    return queue\n"
    )
    return CandidateRef(
        candidate_hash="9f3a",
        payload_directory=directory,
        manifest=CandidateManifest(
            kind="patch",
            target="baseline@0",
            approach="a-01",
            author=WORKER,
            created_at=0.0,
            predicted_score=-115.0,
        ),
    )


def test_slow_tier_passes_the_signal_source_check(
    tmp_path: Path, slow_tier: tuple[SlowTierClient, FakeSlowTierState]
) -> None:
    client, state = slow_tier

    @dataclass(frozen=True, kw_only=True)
    class ServerTimer:
        clock: FakeClock

        def wait_seconds(self, seconds: float) -> None:
            self.clock.advance(seconds)

    check_signal_source(
        SlowTierSource(client=client, declaration=DECLARATION),
        candidate(tmp_path),
        WORKER,
        ServerTimer(clock=state.clock),
    )
    assert set(state.run_headers) == {"r-test"}, "every request names its run"
    assert set(state.key_headers) == {
        "system-key"
    }, "the eval client is a trusted service"
    assert set(state.status_actors) == {"worker-07"}, "status is asked as the submitter"


def test_submission_carries_the_unchanged_payload_and_the_worker(
    tmp_path: Path, slow_tier: tuple[SlowTierClient, FakeSlowTierState]
) -> None:
    client, state = slow_tier
    SlowTierSource(client=client, declaration=DECLARATION).submit(
        candidate(tmp_path), WORKER, "baseline@2", "dev"
    )
    with zipfile.ZipFile(io.BytesIO(state.archives[0])) as archive:
        assert archive.read("policy.py") == (tmp_path / "policy.py").read_bytes()
        manifest: object = yaml.safe_load(archive.read("manifest.yaml"))
    assert manifest == {
        "actor": "worker-07",
        "predicted_score": -115.0,
        "parents": [],
        "direction": "a-01",
        "note": "",
        "target": "baseline@2",
    }


def test_a_duplicate_ticket_is_a_cache_hit(
    tmp_path: Path, slow_tier: tuple[SlowTierClient, FakeSlowTierState]
) -> None:
    """The testbed's duplicate: true means it did not run the candidate (TB section 11.3)."""
    client, state = slow_tier
    source = SlowTierSource(client=client, declaration=DECLARATION)
    first = source.submit(candidate(tmp_path), WORKER, "baseline@0", "dev")
    repeat = source.submit(candidate(tmp_path), WORKER, "baseline@0", "dev")
    state.clock.advance(601.0)
    first_result, repeat_result = source.poll(first), source.poll(repeat)
    assert first_result is not None and not first_result.is_cached
    assert repeat_result is not None and repeat_result.is_cached
    assert repeat_result.per_seed == first_result.per_seed


def test_a_used_up_quota_is_busy_for_the_rest_of_the_run(
    tmp_path: Path, slow_tier: tuple[SlowTierClient, FakeSlowTierState]
) -> None:
    client, state = slow_tier
    state.submissions_left = 0
    state.next_slot_at = None
    with pytest.raises(JudgeBusyError) as busy:
        SlowTierSource(client=client, declaration=DECLARATION).submit(
            candidate(tmp_path), WORKER, "baseline@0", "dev"
        )
    assert busy.value.retry_at_seconds == float("inf")


def test_a_full_hour_becomes_judge_busy(
    tmp_path: Path, slow_tier: tuple[SlowTierClient, FakeSlowTierState]
) -> None:
    client, state = slow_tier
    state.submissions_left = 0
    with pytest.raises(JudgeBusyError) as busy:
        SlowTierSource(client=client, declaration=DECLARATION).submit(
            candidate(tmp_path), WORKER, "baseline@0", "dev"
        )
    assert busy.value.retry_at_seconds == 3600.0


def test_baseline_scores_come_from_meta_then_from_baseline_records(
    slow_tier: tuple[SlowTierClient, FakeSlowTierState],
) -> None:
    client, state = slow_tier
    blackboard = make_store_blackboard(FakeClock(), name="baseline-scores")
    record = BaselineRecord(
        k=3,
        parent=2,
        candidates=("9f3a",),
        per_seed=(-100.0, -99.0, -101.0),
        created_ts=1.0,
    )
    blackboard.write(
        ActorId("integrator"),
        {"baseline/3.json": record.model_dump_json().encode()},
        "baseline 3",
    )
    scores = SlowTierBaselineScores(client=client, blackboard=blackboard)
    assert scores("slow", "baseline@0", "dev") == tuple(BASELINE_PER_SEED)
    assert scores("slow", "baseline@3", "dev") == (-100.0, -99.0, -101.0)
    assert (
        scores("slow", "baseline@4", "dev") is None
    ), "unknown baselines hold the request"


def test_recorder_posts_herd_events(
    slow_tier: tuple[SlowTierClient, FakeSlowTierState]
) -> None:
    client, state = slow_tier
    SlowTierRecorder(client=client).post(
        (
            RecorderEvent(
                ts=1.0,
                run_id="r-test",
                arm="S2",
                actor=WORKER,
                type="blackboard_write",
                data={
                    "kind": "dead_end",
                    "id": "c-1",
                    "refs": ["9f3a"],
                    "hash": "9f3a",
                },
            ),
        )
    )
    assert state.events == [
        {
            "ts": 1.0,
            "run_id": "r-test",
            "arm": "S2",
            "actor": "worker-07",
            "type": "blackboard_write",
            "data": {"kind": "dead_end", "id": "c-1", "refs": ["9f3a"], "hash": "9f3a"},
        }
    ]


def test_a_seed_the_candidate_failed_makes_the_run_void(
    slow_tier: tuple[SlowTierClient, FakeSlowTierState], tmp_path: Path
) -> None:
    """TB section 11.3: a crashed or timed-out seed scores null; that is not an error."""
    client, state = slow_tier
    source = SlowTierSource(client=client, declaration=DECLARATION)
    ticket = source.submit(candidate(tmp_path), WORKER, "baseline@0", "dev")
    state.status_override = {
        "state": "done",
        "result": {
            "score": None,
            "ci": [None, None],
            "n": 3,
            "per_seed": [-110.0, None, -112.0],
            "extra": {"failed_seeds": [1], "reasons": ["timeout"]},
            "constraint_ok": False,
        },
    }
    score = source.poll(ticket)
    assert score is not None
    assert (score.value, score.per_seed, score.constraint_ok) == (None, (), False)
    assert score.extra["reasons"] == ["timeout"]


def test_a_failed_ticket_is_a_judge_failure(
    slow_tier: tuple[SlowTierClient, FakeSlowTierState], tmp_path: Path
) -> None:
    client, state = slow_tier
    source = SlowTierSource(client=client, declaration=DECLARATION)
    ticket = source.submit(candidate(tmp_path), WORKER, "baseline@0", "dev")
    state.status_override = {
        "state": "failed",
        "result": {
            "score": None,
            "n": 0,
            "per_seed": [],
            "extra": {"error": "OSError"},
        },
    }
    with pytest.raises(ValueError, match="could not run"):
        source.poll(ticket)


@pytest.mark.parametrize(
    ("status", "raw", "error_type"),
    [
        (503, b"<html>Service Unavailable</html>", RuntimeError),
        (400, b'{"reason": "missing_prediction"}', ValueError),
        (400, b"<html>Bad Request</html>", ValueError),
    ],
)
def test_server_errors_are_transient_and_refusals_permanent(
    slow_tier: tuple[SlowTierClient, FakeSlowTierState],
    tmp_path: Path,
    status: int,
    raw: bytes,
    error_type: type[Exception],
) -> None:
    """The eval client retries a RuntimeError with backoff and gives up on a ValueError."""
    client, state = slow_tier
    state.submit_override = (status, raw)
    source = SlowTierSource(client=client, declaration=DECLARATION)
    with pytest.raises(error_type):
        source.submit(candidate(tmp_path), WORKER, "baseline@0", "dev")
