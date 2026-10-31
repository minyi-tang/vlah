"""Spec 11.9: quota, visibility, duplicates, header, per-actor share, final."""

import base64
import io
import json
import os
import zipfile

import pytest

from testbed.cli.client import ServiceError, SlowClient, zip_candidate
from testbed.slow_eval.testing import InProcessService
from tests.testbed.helpers import stub_candidate

HOUR_SECONDS = 3600


@pytest.fixture
def service(tmp_path):
    in_process = InProcessService(tmp_path)
    yield in_process
    in_process.close()


def test_missing_run_header_rejected(service):
    service.new_run()
    response = service.http.get("/history")
    assert response.status_code == 400
    response = service.http.post("/submit", json={"zip_b64": ""})
    assert response.status_code == 400


def test_third_submit_in_window_is_429(service, tmp_path):
    service.new_run(quota_per_hour=2, visible_delay_s=600)
    worker = service.client("r1", "w1")
    for i, ingredients in enumerate([["backfill"], ["admission"]]):
        worker.submit_dir(
            stub_candidate(tmp_path / f"c{i}", ingredients), predicted=-900
        )
    with pytest.raises(ServiceError) as error:
        worker.submit_dir(stub_candidate(tmp_path / "c3", ["locality"]), predicted=-900)
    assert error.value.status_code == 429 and error.value.body["reason"] == "quota"
    assert (
        error.value.body["next_slot_at"]
        == service.core.run_row("r1")["t0"] + HOUR_SECONDS
    )
    service.clock.advance(HOUR_SECONDS)
    worker.submit_dir(
        stub_candidate(tmp_path / "c3"), predicted=-900
    )  # first slot of the next window


def test_result_hidden_before_visible_at(service, tmp_path):
    service.new_run(visible_delay_s=600)
    worker = service.client("r1", "w1")
    receipt = worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    status = worker.status(receipt["ticket"])
    assert "result" not in status and status["state"] in ("queued", "running")
    history_item = worker.history()["items"][0]
    assert (
        history_item["score"] is None
        and history_item["ci"] is None
        and history_item["constraint_ok"] is None
    )
    service.clock.advance(599)
    assert "result" not in worker.status(receipt["ticket"])
    service.clock.advance(2)
    status = worker.status(receipt["ticket"])
    assert status["state"] == "done" and status["result"]["score"] == -900
    result_events = [
        event
        for event in service.client("r1", "runner").events()["events"]
        if event["type"] == "result"
    ]
    assert len(result_events) == 1 and result_events[0]["ts"] == receipt["visible_at"]


def test_duplicate_not_charged_but_recorded(service, tmp_path):
    service.new_run(quota_per_hour=2)
    worker = service.client("r1", "w1")
    candidate_dir = stub_candidate(tmp_path / "c", ["backfill"])
    first_receipt = worker.submit_dir(candidate_dir, predicted=-900)
    second_receipt = worker.submit_dir(
        candidate_dir, predicted=-850, note="again"
    )  # still queued/hidden: still a duplicate
    assert (
        second_receipt["duplicate"] is True
        and second_receipt["of"] == first_receipt["ticket"]
    )
    assert worker.history()["quota"]["used_total"] == 1
    submits = [
        event
        for event in service.client("r1", "runner").events()["events"]
        if event["type"] == "submit"
    ]
    assert [submit["data"]["duplicate"] for submit in submits] == [False, True]
    worker.submit_dir(
        stub_candidate(tmp_path / "c2", ["admission"]), predicted=-900
    )  # second slot still free


def test_same_files_other_target_is_a_new_candidate(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    candidate_dir = stub_candidate(tmp_path / "c", ["backfill"])
    first_receipt = worker.submit_dir(
        candidate_dir, predicted=-900, target="baseline@0"
    )
    second_receipt = worker.submit_dir(
        candidate_dir, predicted=-900, target="baseline@1"
    )
    assert (
        "duplicate" not in second_receipt
        and first_receipt["ticket"] != second_receipt["ticket"]
    )


def test_missing_prediction_and_too_large(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    candidate_dir = stub_candidate(tmp_path / "c", [])
    with pytest.raises(ServiceError) as error:
        worker.submit(zip_candidate(candidate_dir))
    assert error.value.body["reason"] == "missing_prediction"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("blob.bin", os.urandom(6 * 1024 * 1024))
    with pytest.raises(ServiceError) as error:
        worker.submit(base64.b64encode(buffer.getvalue()).decode())
    assert error.value.body["reason"] == "too_large"


def test_per_actor_share_isolates_history_and_quota(service, tmp_path):
    service.new_run(arm="isolated", n=4, quota_per_hour=2, per_actor_share=True)
    first_worker, second_worker = service.client("r1", "w1"), service.client("r1", "w2")
    candidate_dir = stub_candidate(tmp_path / "c", ["backfill"])
    first_receipt = first_worker.submit_dir(candidate_dir, predicted=-900)
    second_receipt = second_worker.submit_dir(
        candidate_dir, predicted=-900
    )  # same files, different actor: not a duplicate of w1's
    assert "duplicate" not in second_receipt
    assert [item["actor"] for item in first_worker.history()["items"]] == ["w1"]
    with pytest.raises(ServiceError) as error:
        second_worker.status(first_receipt["ticket"])
    assert error.value.status_code == 404
    with pytest.raises(ServiceError) as error:  # 1 per ceil(4/2)=2 h window
        first_worker.submit_dir(
            stub_candidate(tmp_path / "c2", ["admission"]), predicted=-900
        )
    assert error.value.status_code == 429
    service.clock.advance(2 * HOUR_SECONDS)
    first_worker.submit_dir(
        stub_candidate(tmp_path / "c2", ["admission"]), predicted=-900
    )


def test_audit_and_judge_only_for_runner(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    with pytest.raises(ServiceError) as error:
        worker.submit_dir(
            stub_candidate(tmp_path / "c", ["backfill"]),
            predicted=-900,
            dataset="audit",
        )
    assert error.value.status_code == 403
    receipt = service.client("r1", "runner").submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900, dataset="audit"
    )
    assert receipt["ticket"]


def test_calibration_and_meta_hide_reference(service):
    service.new_run()
    worker = service.client("r1", "w1")
    meta = worker.meta()
    assert meta["baseline"]["score"] == -1000 and "reference" not in str(
        meta
    ).lower().replace("_", "")
    calibration = [
        event
        for event in service.client("r1", "runner").events()["events"]
        if event["type"] == "calibration"
    ][0]["data"]
    assert calibration["reference_slow"]["score"] == -500
    for call in (
        worker.calibration,
        worker.events,
        lambda: worker.candidate("x"),
        lambda: worker.final_check("t-000001"),
    ):
        with pytest.raises(ServiceError) as error:
            call()
        assert error.value.status_code == 403


def test_final_runner_vs_actor(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    receipt = worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    assert worker.final(ticket=receipt["ticket"])["recorded"] == "actor_final"
    final = service.client("r1", "runner").final(
        ticket=receipt["ticket"]
    )  # forces visibility for the run-level final
    assert final["recorded"] == "final"
    types = [
        event["type"] for event in service.client("r1", "runner").events()["events"]
    ]
    assert (
        types.count("actor_final") == 1
        and types.count("final") == 1
        and "result" in types
    )


def test_anomaly_flag(service, tmp_path):
    service.new_run(visible_delay_s=0)
    worker = service.client("r1", "w1")
    worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"], dev_only_bonus=5000),
        predicted=-900,
    )
    service.clock.advance(1)
    assert any(
        event["type"] == "anomaly"
        for event in service.client("r1", "runner").events()["events"]
    )


def test_final_check_is_runner_only(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    receipt = worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    with pytest.raises(ServiceError) as error:
        worker.final_check(receipt["ticket"], dataset="audit")
    assert error.value.status_code == 403
    runner = service.client("r1", "runner")
    with pytest.raises(ServiceError) as error:
        runner.final_check(receipt["ticket"], dataset="judge")
    assert error.value.status_code == 400
    assert runner.final_check(receipt["ticket"])["dataset"] == "public"


def test_workers_cannot_act_as_the_testbed(service, tmp_path):
    service.new_run(quota_per_hour=2)
    spoof = service.client(
        "r1", "runner", key=""
    )  # a worker claiming to be the runner, no key
    with pytest.raises(ServiceError) as error:
        spoof.submit_dir(stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900)
    assert error.value.status_code == 403
    for call in (
        spoof.history,
        spoof.calibration,
        lambda: spoof.final(ticket="t-000001"),
        lambda: spoof.create_run("herd", "stub", 1),
    ):
        with pytest.raises(ServiceError) as error:
            call()
        assert error.value.status_code == 403
    worker = service.client("r1", "w1")
    for event in (
        {"type": "final", "data": {"hash": "h", "ticket": "t"}},
        {"type": "result", "data": {}},
        {"type": "calibration", "data": {}},
    ):
        with pytest.raises(ServiceError) as error:
            worker.post_events([event])
        assert error.value.status_code == 403
    worker.post_events(
        [
            {
                "type": "fast_run",
                "actor": "w2",
                "data": {"hash": "h", "seed": 1, "score": 1, "baseline_score": 0},
            }
        ]
    )
    fast_runs = [
        event
        for event in service.client("r1", "runner").events()["events"]
        if event["type"] == "fast_run"
    ]
    assert fast_runs[0]["actor"] == "w1"  # workers post only as themselves


def test_runner_dev_submits_are_capped(service, tmp_path):
    service.new_run()
    runner = service.client("r1", "runner")
    for i in range(6):
        runner.submit_dir(
            stub_candidate(tmp_path / f"c{i}", [], bonus={"x": i}), predicted=-1000
        )
    with pytest.raises(ServiceError) as error:
        runner.submit_dir(
            stub_candidate(tmp_path / "c7", [], bonus={"x": 7}), predicted=-1000
        )
    assert error.value.status_code == 429


def test_failed_ticket_does_not_block_resubmit(service, tmp_path):
    service.new_run()
    worker = service.client("r1", "w1")
    candidate_dir = stub_candidate(tmp_path / "c", ["backfill"])
    first_receipt = worker.submit_dir(candidate_dir, predicted=-900)
    service.core.db.execute(
        "UPDATE tickets SET state='failed' WHERE ticket=?", (first_receipt["ticket"],)
    )
    second_receipt = worker.submit_dir(candidate_dir, predicted=-900)
    assert "duplicate" not in second_receipt


def test_per_actor_share_single_actor_is_one_per_hour(service, tmp_path):
    service.new_run(arm="isolated", n=1, quota_per_hour=2, per_actor_share=True)
    worker = service.client("r1", "w1")
    worker.submit_dir(stub_candidate(tmp_path / "a", ["backfill"]), predicted=-900)
    with pytest.raises(ServiceError) as error:
        worker.submit_dir(stub_candidate(tmp_path / "b", ["admission"]), predicted=-900)
    assert error.value.status_code == 429


def test_per_agent_tokens_bind_an_agent_to_itself(service, tmp_path):
    runner = service.client("r1", "runner")
    runner.create_run(
        "isolated",
        "stub",
        2,
        {"per_actor_share": True},
        actor_tokens={"w1": "tok-1", "w2": "tok-2"},
    )
    first_worker = SlowClient(None, "r1", "w1", http=service.http, token="tok-1")
    receipt = first_worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    assert receipt["ticket"] and first_worker.history()["items"]
    spoof = SlowClient(
        None, "r1", "w1", http=service.http, token="tok-2"
    )  # w2 claiming to be w1
    for call in (
        spoof.history,
        lambda: spoof.status(receipt["ticket"]),
        lambda: spoof.submit_dir(
            stub_candidate(tmp_path / "d", ["admission"]), predicted=-900
        ),
        lambda: spoof.post_events(
            [
                {
                    "type": "fast_run",
                    "data": {"hash": "h", "seed": 1, "score": 1, "baseline_score": 0},
                }
            ]
        ),
    ):
        with pytest.raises(ServiceError) as error:
            call()
        assert error.value.status_code == 403
    # a trusted client (the herd's Eval service, issue #10 3a) submits for any worker via manifest.actor
    trusted = SlowClient(
        None, "r1", "w2", http=service.http, key=service.core.system_key
    )
    assert trusted.submit_dir(
        stub_candidate(tmp_path / "e", ["locality"]), predicted=-900
    )["ticket"]


def test_operator_registers_worker_tokens_after_the_run_exists(service, tmp_path):
    runner = service.new_run(
        arm="herd", n=3
    )  # created with no tokens, as tb run does for the herd
    registration = runner.register_actors({"worker-01": "tok-1", "worker-02": "tok-2"})
    assert registration == {
        "run_id": "r1",
        "added": ["worker-01", "worker-02"],
        "unchanged": [],
        "actors": ["worker-01", "worker-02"],
        "scopes": {"worker-01": "full", "worker-02": "full"},
    }
    assert "tok-" not in json.dumps(registration)
    first_worker = SlowClient(None, "r1", "worker-01", http=service.http, token="tok-1")
    receipt = first_worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    first_worker.status(receipt["ticket"])
    impostors = [
        SlowClient(None, "r1", "worker-01", http=service.http, token="tok-2"),
        SlowClient(None, "r1", "worker-03", http=service.http, token="tok-3"),
        SlowClient(None, "r1", "worker-01", http=service.http),
    ]
    for impostor in impostors:
        with pytest.raises(ServiceError) as error:
            impostor.history()
        assert error.value.status_code == 403
    repeated = runner.register_actors({"worker-01": "tok-1", "worker-03": "tok-3"})
    assert repeated["added"] == ["worker-03"]
    assert repeated["unchanged"] == ["worker-01"]
    assert repeated["actors"] == ["worker-01", "worker-02", "worker-03"]
    assert repeated["scopes"] == {"worker-01": "full", "worker-03": "full"}
    late_worker = SlowClient(None, "r1", "worker-03", http=service.http, token="tok-3")
    history_items = late_worker.history()["items"]
    late_tickets = [item["ticket"] for item in history_items if isinstance(item, dict)]
    assert late_tickets == [receipt["ticket"]]


def test_only_the_system_key_registers_actors(service):
    service.new_run(arm="herd")
    for key, token in ((None, None), ("wrong-key", None), (None, "tok-1")):
        caller = SlowClient(
            None, "r1", "worker-01", http=service.http, key=key, token=token
        )
        with pytest.raises(ServiceError) as error:
            caller.register_actors({"worker-01": "tok-1"})
        assert error.value.status_code == 403
        assert error.value.body == {"reason": "testbed only"}
    assert json.loads(service.core.run_row("r1")["tokens"]) == {}
    with pytest.raises(ServiceError) as error:
        service.client("r-unknown", "runner").register_actors({"worker-01": "tok-1"})
    assert error.value.status_code == 404


def test_actor_registration_rejects_replacements_and_bad_input_unchanged(service):
    runner = service.new_run(arm="herd")
    runner.register_actors({"worker-01": "tok-1"})

    def post_actors(body):
        return service.http.post("/actors", json=body, headers=runner.headers())

    conflicts = [
        ({"worker-01": "tok-other", "worker-02": "tok-2"}, ["worker-01"]),
        ({"worker-02": "tok-1"}, ["worker-02"]),  # token held by worker-01
        ({"worker-02": "tok-x", "worker-03": "tok-x"}, ["worker-03"]),
    ]
    for actor_tokens, conflicting_actors in conflicts:
        response = post_actors({"actor_tokens": actor_tokens})
        assert response.status_code == 409
        assert response.json() == {
            "reason": "actor_token_conflict",
            "actors": conflicting_actors,
        }
    invalid_bodies = [
        {},
        {"actor_tokens": {}},
        {"actor_tokens": ["worker-02"]},
        {"actor_tokens": {"worker-02": ""}},
        {"actor_tokens": {"worker-02": 7}},
        {"actor_tokens": {"": "tok-2"}},
        {"actor_tokens": {"worker-02": "tok-2", "runner": "tok-r"}},
        {"actor_tokens": {"composer": "tok-c"}},
    ]
    for body in invalid_bodies:
        response = post_actors(body)
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_actor_tokens"
    assert json.loads(service.core.run_row("r1")["tokens"]) == {"worker-01": "tok-1"}


def test_fast_scoped_tokens_reach_only_the_fast_tier(service, tmp_path):
    runner = service.new_run(arm="herd", n=2)
    registration = runner.register_actors(
        {"worker-01": "tok-1", "worker-02": "tok-2"}, scope="fast"
    )
    assert registration["scopes"] == {"worker-01": "fast", "worker-02": "fast"}
    worker = SlowClient(None, "r1", "worker-01", http=service.http, token="tok-1")
    worker.fast(
        zip_candidate(stub_candidate(tmp_path / "f", ["backfill"])), 1, "baseline@0"
    )
    assert worker.meta()
    # the herd's Eval client still spends judge runs for the worker with the system key
    eval_client = SlowClient(
        None, "r1", "worker-01", http=service.http, key=service.core.system_key
    )
    receipt = eval_client.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    assert service.core.ticket_row("r1", receipt["ticket"])["actor"] == "worker-01"
    denied_calls = {
        "/submit": lambda: worker.submit_dir(
            stub_candidate(tmp_path / "d", ["admission"]), predicted=-900
        ),
        "/status": lambda: worker.status(receipt["ticket"]),
        "/history": worker.history,
        "/final": lambda: worker.final(ticket=receipt["ticket"]),
        "/events": lambda: worker.post_events(
            [
                {
                    "type": "fast_run",
                    "data": {"hash": "h", "seed": 1, "score": 1, "baseline_score": 0},
                }
            ]
        ),
    }
    for route, call in denied_calls.items():
        with pytest.raises(ServiceError) as error:
            call()
        assert error.value.status_code == 403
        assert error.value.body == {
            "reason": f"token scope 'fast' does not allow {route}"
        }
    ticket_count = service.core.db.execute(
        "SELECT COUNT(*) FROM tickets WHERE run_id='r1'"
    ).fetchone()[0]
    assert ticket_count == 1


def test_actor_scope_is_bound_at_registration(service):
    runner = service.new_run(arm="herd")
    runner.register_actors({"worker-01": "tok-1"}, scope="fast")
    repeated = runner.register_actors({"worker-01": "tok-1"}, scope="fast")
    assert repeated["unchanged"] == ["worker-01"]
    assert repeated["scopes"] == {"worker-01": "fast"}

    def post_actors(body):
        return service.http.post("/actors", json=body, headers=runner.headers())

    for body in (
        {"actor_tokens": {"worker-01": "tok-1"}, "scope": "full"},
        {"actor_tokens": {"worker-01": "tok-1"}},  # no scope means full
    ):
        response = post_actors(body)
        assert response.status_code == 409
        assert response.json() == {
            "reason": "actor_token_conflict",
            "actors": ["worker-01"],
        }
    for scope in ("admin", "FAST", "", 7, None, ["fast"]):
        response = post_actors({"actor_tokens": {"worker-02": "tok-2"}, "scope": scope})
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_scope"
    run = service.core.run_row("r1")
    assert json.loads(run["tokens"]) == {"worker-01": "tok-1"}
    assert json.loads(run["scopes"]) == {"worker-01": "fast"}
