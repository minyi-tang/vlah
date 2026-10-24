"""Issue #113: the fast tier is served by the slow-eval service inside a run (POST /fast), which
records the fast_run event itself; tb fast calls it when TB_SLOW_URL and TB_RUN_ID are set and
evaluates locally otherwise."""

import json
import threading
from pathlib import Path

import pytest

from testbed.cli.client import ServiceError, SlowClient, zip_candidate
from testbed.cli.main import main as tb_main
from testbed.problem_api import (
    POOLS,
    PROBLEMS_ROOT,
    candidate_hash,
    hidden_root,
    load_problem,
)
from testbed.recorder.log import read_events
from testbed.slow_eval.testing import InProcessService
from tests.testbed.helpers import stub_candidate

RESPONSE_KEYS = {
    "hash",
    "seed",
    "score",
    "baseline_score",
    "per_item",
    "extra",
    "elapsed_s",
}
STUB_BASELINE_SCORE = -1000.0  # the stub policy declares no ingredient
BARRIER_TIMEOUT_SECONDS = 10


@pytest.fixture
def service(tmp_path):
    in_process = InProcessService(tmp_path)
    yield in_process
    in_process.close()


def fast_run_events(service: InProcessService, run_id: str = "r1") -> list[dict]:
    events_path = service.core.data / "runs" / run_id / "events.jsonl"
    return [event for event in read_events(events_path) if event["type"] == "fast_run"]


def test_fast_scores_and_records_one_event_without_a_ticket(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    candidate_dir = stub_candidate(tmp_path / "c", ["backfill"])
    response = worker.fast(zip_candidate(candidate_dir), 2, "baseline@3")
    assert set(response) == RESPONSE_KEYS
    assert response["seed"] == 2 and response["score"] == -900.0
    assert response["baseline_score"] == STUB_BASELINE_SCORE
    assert response["hash"] == candidate_hash(candidate_dir, "baseline@3")
    assert response["per_item"] == [{"id": "total", "value": -900.0}]
    events = fast_run_events(service)
    assert len(events) == 1 and events[0]["actor"] == "w1"
    assert events[0]["data"] == {
        "hash": response["hash"],
        "seed": 2,
        "score": -900.0,
        "baseline_score": STUB_BASELINE_SCORE,
    }
    assert worker.history()["items"] == []
    assert not list((service.core.data / "runs" / "r1" / "fast").iterdir())


def test_fast_rejects_a_non_public_seed_and_records_nothing(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    zip_b64 = zip_candidate(stub_candidate(tmp_path / "c", ["backfill"]))
    for seed in (11, "1", None):
        with pytest.raises(ServiceError) as error:
            worker.fast(zip_b64, seed, "baseline@0")
        assert error.value.status_code == 400
        assert error.value.body["reason"] == "invalid_seed"
    assert fast_run_events(service) == []


def test_fast_binds_the_actor_to_its_token(service, tmp_path):
    runner = service.client("r1", "runner")
    runner.create_run(
        "isolated", "stub", 2, {}, actor_tokens={"w1": "tok-1", "w2": "tok-2"}
    )
    zip_b64 = zip_candidate(stub_candidate(tmp_path / "c", ["backfill"]))
    spoof = SlowClient(None, "r1", "w1", http=service.http, token="tok-2")
    with pytest.raises(ServiceError) as error:
        spoof.fast(zip_b64, 1, "baseline@0")
    assert error.value.status_code == 403
    for actor in ("runner", None):
        anonymous = SlowClient(None, "r1", actor or "anon", http=service.http)
        with pytest.raises(ServiceError) as error:
            anonymous.fast(zip_b64, 1, "baseline@0")
        assert error.value.status_code == 403
    assert fast_run_events(service) == []
    worker = SlowClient(None, "r1", "w1", http=service.http, token="tok-1")
    worker.fast(zip_b64, 1, "baseline@0")
    assert [event["actor"] for event in fast_run_events(service)] == ["w1"]


def test_two_fast_requests_evaluate_at_the_same_time(service, tmp_path, monkeypatch):
    """The semaphore admits two evaluations at once: each request waits inside the problem's
    evaluate for the other, so a service that serialized them would break the barrier.
    """
    service.new_run()
    problem = service.core.problem("stub")
    inside_evaluate = threading.Barrier(2, timeout=BARRIER_TIMEOUT_SECONDS)
    real_evaluate = problem.evaluate

    def evaluate_after_meeting(candidate_dir, tier, seed, timeout=True):
        inside_evaluate.wait()
        return real_evaluate(candidate_dir, tier, seed, timeout)

    monkeypatch.setattr(problem, "evaluate", evaluate_after_meeting)
    responses: dict[str, dict] = {}
    errors: list[BaseException] = []

    def request(actor: str, ingredient: str):
        try:
            zip_b64 = zip_candidate(stub_candidate(tmp_path / actor, [ingredient]))
            responses[actor] = service.client("r1", actor).fast(
                zip_b64, 1, "baseline@0"
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=request, args=("w1", "backfill")),
        threading.Thread(target=request, args=("w2", "admission")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=BARRIER_TIMEOUT_SECONDS * 2)
    assert not errors, errors
    assert {actor: response["score"] for actor, response in responses.items()} == {
        "w1": -900.0,
        "w2": -900.0,
    }
    assert sorted(event["actor"] for event in fast_run_events(service)) == ["w1", "w2"]


needs_hidden = pytest.mark.skipif(
    hidden_root("sched") is None,
    reason="hidden material (private/testbed/sched) is not here",
)


@pytest.mark.slow
@needs_hidden
@pytest.mark.timeout(900)
def test_fast_on_problem_a_matches_the_local_fast_tier_without_mechanisms(
    service, tmp_path
):
    """The service's number for a public seed is the local fast tier's number, and the response
    carries no mechanism counters or job waits."""
    service.new_run(problem="sched", slow_seeds=1)
    sched = load_problem("sched")
    local = sched.evaluate(sched.baseline_dir(), "fast", 1)
    response = service.client("r1", "w1").fast(
        zip_candidate(sched.baseline_dir()), 1, "baseline@0"
    )
    assert response["score"] == local["score"]
    assert response["baseline_score"] == local["score"]
    assert response["per_item"] == local["per_item"]
    assert "mechanisms" not in response["extra"] and "waits" not in response["extra"]
    assert "mechanisms" in local["extra"]
    assert "fast" in POOLS  # the request ran on its own pool, not the slow batch's


# ------------------------------------------------------------------------ tb fast

CANNED_RESPONSE = {
    "hash": "abcdef0123456789" * 4,
    "seed": 2,
    "score": -850.5,
    "baseline_score": -1000.0,
    "per_item": [{"id": "total", "value": -850.5}],
    "extra": {"dataset": "public"},
    "elapsed_s": 3.5,
}
EXPECTED_LINES = [
    "score -850.5   baseline -1000.0   (seed 2, 3.5s)   hash abcdef012345",
    "  total        -850.5",
]


class FakeSlowClient:
    calls: list[tuple[str, int, str]] = []
    error: ServiceError | None = None

    @classmethod
    def from_env(cls):
        return cls()

    def fast(self, zip_b64, seed: int, target: str):
        FakeSlowClient.calls.append((zip_b64, seed, target))
        if FakeSlowClient.error is not None:
            raise FakeSlowClient.error
        else:
            pass
        return dict(CANNED_RESPONSE)


@pytest.fixture
def worker_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TB_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("TB_PROBLEM_DIR", str(PROBLEMS_ROOT / "stub"))
    monkeypatch.delenv("TB_SLOW_URL", raising=False)
    monkeypatch.delenv("TB_RUN_ID", raising=False)
    monkeypatch.delenv("TB_TARGET", raising=False)
    monkeypatch.delenv("TB_TARGET_FILE", raising=False)
    FakeSlowClient.calls = []
    FakeSlowClient.error = None
    return stub_candidate(tmp_path / "work", ["backfill"])


def test_tb_fast_inside_a_run_prints_the_service_result(
    worker_environment, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TB_SLOW_URL", "http://slow.test")
    monkeypatch.setenv("TB_RUN_ID", "r1")
    monkeypatch.setattr("testbed.cli.client.SlowClient", FakeSlowClient)
    out_path = tmp_path / "fast.json"
    tb_main(["fast", str(worker_environment), "--seed", "2", "--out", str(out_path)])
    assert capsys.readouterr().out.splitlines() == EXPECTED_LINES
    assert json.loads(out_path.read_text()) == CANNED_RESPONSE
    [(zip_b64, seed, target)] = FakeSlowClient.calls
    assert zip_b64 == zip_candidate(worker_environment)
    assert (seed, target) == (2, "baseline@0")
    assert not (Path.home() / ".tb" / "events.jsonl").exists()


def test_tb_fast_inside_a_run_fails_instead_of_evaluating_locally(
    worker_environment, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("TB_SLOW_URL", "http://slow.test")
    monkeypatch.setenv("TB_RUN_ID", "r1")
    monkeypatch.setattr("testbed.cli.client.SlowClient", FakeSlowClient)
    FakeSlowClient.error = ServiceError(503, {"reason": "down"})
    out_path = tmp_path / "fast.json"
    with pytest.raises(SystemExit) as exit_info:
        tb_main(
            ["fast", str(worker_environment), "--seed", "2", "--out", str(out_path)]
        )
    assert "503" in str(
        exit_info.value.code
    )  # sys.exit(message) exits 1 with that message
    assert capsys.readouterr().out == ""
    assert not out_path.exists()


def test_tb_fast_outside_a_run_evaluates_locally(worker_environment, tmp_path, capsys):
    out_path = tmp_path / "fast.json"
    tb_main(["fast", str(worker_environment), "--seed", "2", "--out", str(out_path)])
    expected_hash = candidate_hash(worker_environment, "baseline@0")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("score -900.0   baseline -1000.0   (seed 2, ")
    assert lines[0].endswith(f"hash {expected_hash[:12]}")
    assert lines[1:] == ["  total        -900.0"]
    written = json.loads(out_path.read_text())
    assert written["hash"] == expected_hash and written["score"] == -900.0
    backup_events = read_events(Path.home() / ".tb" / "events.jsonl")
    assert [event["type"] for event in backup_events] == ["fast_run"]
    assert backup_events[0]["data"]["hash"] == expected_hash
    assert FakeSlowClient.calls == []
