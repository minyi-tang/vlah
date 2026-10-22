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
    archives: list[bytes] = field(default_factory=list[bytes])
    run_headers: list[str] = field(default_factory=list[str])
    key_headers: list[str] = field(default_factory=list[str])
    status_actors: list[str] = field(default_factory=list[str])
    events: list[object] = field(default_factory=list[object])


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
                if state.clock.now_seconds() >= state.tickets[ticket]:
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
                if state.submissions_left <= 0:
                    self.reply(
                        429, {"reason": "quota", "next_slot_at": state.next_slot_at}
                    )
                else:
                    state.submissions_left -= 1
                    state.archives.append(base64.b64decode(str(body["zip_b64"])))
                    ticket = f"ticket-{len(state.tickets) + 1}"
                    state.tickets[ticket] = state.clock.now_seconds() + 600.0
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
