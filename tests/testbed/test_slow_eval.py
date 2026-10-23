"""Spec 11.9: quota, visibility, duplicates, header, per-actor share, final."""

import pytest

from testbed.cli.client import ServiceError
from testbed.slow_eval.testing import InProcessService
from tests.testbed.helpers import stub_candidate


@pytest.fixture
def svc(tmp_path):
    service = InProcessService(tmp_path)
    yield service
    service.close()


def test_missing_run_header_rejected(svc):
    svc.new_run()
    r = svc.http.get("/history")
    assert r.status_code == 400
    r = svc.http.post("/submit", json={"zip_b64": ""})
    assert r.status_code == 400


def test_third_submit_in_window_is_429(svc, tmp_path):
    svc.new_run(quota_per_hour=2, visible_delay_s=600)
    w = svc.client("r1", "w1")
    for i, ings in enumerate([["backfill"], ["admission"]]):
        w.submit_dir(stub_candidate(tmp_path / f"c{i}", ings), predicted=-900)
    with pytest.raises(ServiceError) as e:
        w.submit_dir(stub_candidate(tmp_path / "c3", ["locality"]), predicted=-900)
    assert e.value.code == 429 and e.value.body["reason"] == "quota"
    assert e.value.body["next_slot_at"] == svc.core.run_row("r1")["t0"] + 3600
    svc.clock.advance(3600)
    w.submit_dir(
        stub_candidate(tmp_path / "c3"), predicted=-900
    )  # first slot of the next window


def test_result_hidden_before_visible_at(svc, tmp_path):
    svc.new_run(visible_delay_s=600)
    w = svc.client("r1", "w1")
    r = w.submit_dir(stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900)
    st = w.status(r["ticket"])
    assert "result" not in st and st["state"] in ("queued", "running")
    hist = w.history()["items"][0]
    assert (
        hist["score"] is None and hist["ci"] is None and hist["constraint_ok"] is None
    )
    svc.clock.advance(599)
    assert "result" not in w.status(r["ticket"])
    svc.clock.advance(2)
    st = w.status(r["ticket"])
    assert st["state"] == "done" and st["result"]["score"] == -900
    ev = [
        e
        for e in svc.client("r1", "runner").events()["events"]
        if e["type"] == "result"
    ]
    assert len(ev) == 1 and ev[0]["ts"] == r["visible_at"]


def test_duplicate_not_charged_but_recorded(svc, tmp_path):
    svc.new_run(quota_per_hour=2)
    w = svc.client("r1", "w1")
    d = stub_candidate(tmp_path / "c", ["backfill"])
    r1 = w.submit_dir(d, predicted=-900)
    r2 = w.submit_dir(
        d, predicted=-850, note="again"
    )  # still queued/hidden: still a duplicate
    assert r2["duplicate"] is True and r2["of"] == r1["ticket"]
    assert w.history()["quota"]["used_total"] == 1
    subs = [
        e
        for e in svc.client("r1", "runner").events()["events"]
        if e["type"] == "submit"
    ]
    assert [s["data"]["duplicate"] for s in subs] == [False, True]
    w.submit_dir(
        stub_candidate(tmp_path / "c2", ["admission"]), predicted=-900
    )  # second slot still free


def test_same_files_other_target_is_a_new_candidate(svc, tmp_path):
    svc.new_run()
    w = svc.client("r1", "w1")
    d = stub_candidate(tmp_path / "c", ["backfill"])
    a = w.submit_dir(d, predicted=-900, target="baseline@0")
    b = w.submit_dir(d, predicted=-900, target="baseline@1")
    assert "duplicate" not in b and a["ticket"] != b["ticket"]


def test_missing_prediction_and_too_large(svc, tmp_path):
    import base64
    import io
    import zipfile

    svc.new_run()
    w = svc.client("r1", "w1")
    d = stub_candidate(tmp_path / "c", [])
    from testbed.cli.client import zip_candidate

    with pytest.raises(ServiceError) as e:
        w.submit(zip_candidate(d))
    assert e.value.body["reason"] == "missing_prediction"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        import os

        zf.writestr("blob.bin", os.urandom(6 * 1024 * 1024))
    with pytest.raises(ServiceError) as e:
        w.submit(base64.b64encode(buf.getvalue()).decode())
    assert e.value.body["reason"] == "too_large"


def test_per_actor_share_isolates_history_and_quota(svc, tmp_path):
    svc.new_run(arm="isolated", n=4, quota_per_hour=2, per_actor_share=True)
    a, b = svc.client("r1", "w1"), svc.client("r1", "w2")
    d = stub_candidate(tmp_path / "c", ["backfill"])
    ra = a.submit_dir(d, predicted=-900)
    rb = b.submit_dir(
        d, predicted=-900
    )  # same files, different actor: not a duplicate of w1's
    assert "duplicate" not in rb
    assert [i["actor"] for i in a.history()["items"]] == ["w1"]
    with pytest.raises(ServiceError) as e:
        b.status(ra["ticket"])
    assert e.value.code == 404
    with pytest.raises(ServiceError) as e:  # 1 per ceil(4/2)=2 h window
        a.submit_dir(stub_candidate(tmp_path / "c2", ["admission"]), predicted=-900)
    assert e.value.code == 429
    svc.clock.advance(2 * 3600)
    a.submit_dir(stub_candidate(tmp_path / "c2", ["admission"]), predicted=-900)


def test_audit_and_judge_only_for_runner(svc, tmp_path):
    svc.new_run()
    w = svc.client("r1", "w1")
    with pytest.raises(ServiceError) as e:
        w.submit_dir(
            stub_candidate(tmp_path / "c", ["backfill"]),
            predicted=-900,
            dataset="audit",
        )
    assert e.value.code == 403
    r = svc.client("r1", "runner").submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900, dataset="audit"
    )
    assert r["ticket"]


def test_calibration_and_meta_hide_reference(svc):
    svc.new_run()
    w = svc.client("r1", "w1")
    meta = w.meta()
    assert meta["baseline"]["score"] == -1000 and "reference" not in str(
        meta
    ).lower().replace("_", "")
    cal = [
        e
        for e in svc.client("r1", "runner").events()["events"]
        if e["type"] == "calibration"
    ][0]["data"]
    assert cal["reference_slow"]["score"] == -500
    for call in (
        w.calibration,
        w.events,
        lambda: w.candidate("x"),
        lambda: w.final_check("t-000001"),
    ):
        with pytest.raises(ServiceError) as e:
            call()
        assert e.value.code == 403


def test_final_runner_vs_actor(svc, tmp_path):
    svc.new_run()
    w = svc.client("r1", "w1")
    r = w.submit_dir(stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900)
    assert w.final(ticket=r["ticket"])["recorded"] == "actor_final"
    out = svc.client("r1", "runner").final(
        ticket=r["ticket"]
    )  # forces visibility for the run-level final
    assert out["recorded"] == "final"
    types = [e["type"] for e in svc.client("r1", "runner").events()["events"]]
    assert (
        types.count("actor_final") == 1
        and types.count("final") == 1
        and "result" in types
    )


def test_anomaly_flag(svc, tmp_path):
    svc.new_run(visible_delay_s=0)
    w = svc.client("r1", "w1")
    w.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"], dev_only_bonus=5000),
        predicted=-900,
    )
    svc.clock.advance(1)
    assert any(
        e["type"] == "anomaly" for e in svc.client("r1", "runner").events()["events"]
    )


def test_final_check_is_runner_only(svc, tmp_path):
    svc.new_run()
    w = svc.client("r1", "w1")
    r = w.submit_dir(stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900)
    with pytest.raises(ServiceError) as e:
        w.final_check(r["ticket"], dataset="audit")
    assert e.value.code == 403
    runner = svc.client("r1", "runner")
    with pytest.raises(ServiceError) as e:
        runner.final_check(r["ticket"], dataset="judge")
    assert e.value.code == 400
    assert runner.final_check(r["ticket"])["dataset"] == "public"


def test_workers_cannot_act_as_the_testbed(svc, tmp_path):
    svc.new_run(quota_per_hour=2)
    spoof = svc.client(
        "r1", "runner", key=""
    )  # a worker claiming to be the runner, no key
    with pytest.raises(ServiceError) as e:
        spoof.submit_dir(stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900)
    assert e.value.code == 403
    for call in (
        spoof.history,
        spoof.calibration,
        lambda: spoof.final(ticket="t-000001"),
        lambda: spoof.create_run("herd", "stub", 1),
    ):
        with pytest.raises(ServiceError) as e:
            call()
        assert e.value.code == 403
    w = svc.client("r1", "w1")
    for ev in (
        {"type": "final", "data": {"hash": "h", "ticket": "t"}},
        {"type": "result", "data": {}},
        {"type": "calibration", "data": {}},
    ):
        with pytest.raises(ServiceError) as e:
            w.post_events([ev])
        assert e.value.code == 403
    w.post_events(
        [
            {
                "type": "fast_run",
                "actor": "w2",
                "data": {"hash": "h", "seed": 1, "score": 1, "baseline_score": 0},
            }
        ]
    )
    fr = [
        e
        for e in svc.client("r1", "runner").events()["events"]
        if e["type"] == "fast_run"
    ]
    assert fr[0]["actor"] == "w1"  # workers post only as themselves


def test_runner_dev_submits_are_capped(svc, tmp_path):
    svc.new_run()
    runner = svc.client("r1", "runner")
    for i in range(6):
        runner.submit_dir(
            stub_candidate(tmp_path / f"c{i}", [], bonus={"x": i}), predicted=-1000
        )
    with pytest.raises(ServiceError) as e:
        runner.submit_dir(
            stub_candidate(tmp_path / "c7", [], bonus={"x": 7}), predicted=-1000
        )
    assert e.value.code == 429


def test_failed_ticket_does_not_block_resubmit(svc, tmp_path):
    svc.new_run()
    w = svc.client("r1", "w1")
    d = stub_candidate(tmp_path / "c", ["backfill"])
    r1 = w.submit_dir(d, predicted=-900)
    svc.core.db.execute(
        "UPDATE tickets SET state='failed' WHERE ticket=?", (r1["ticket"],)
    )
    r2 = w.submit_dir(d, predicted=-900)
    assert "duplicate" not in r2


def test_per_actor_share_single_actor_is_one_per_hour(svc, tmp_path):
    svc.new_run(arm="isolated", n=1, quota_per_hour=2, per_actor_share=True)
    w = svc.client("r1", "w1")
    w.submit_dir(stub_candidate(tmp_path / "a", ["backfill"]), predicted=-900)
    with pytest.raises(ServiceError) as e:
        w.submit_dir(stub_candidate(tmp_path / "b", ["admission"]), predicted=-900)
    assert e.value.code == 429


def test_per_agent_tokens_bind_an_agent_to_itself(svc, tmp_path):
    runner = svc.client("r1", "runner")
    runner.create_run(
        "isolated",
        "stub",
        2,
        {"per_actor_share": True},
        actor_tokens={"w1": "tok-1", "w2": "tok-2"},
    )
    from testbed.cli.client import SlowClient

    w1 = SlowClient(None, "r1", "w1", http=svc.http, token="tok-1")
    r = w1.submit_dir(stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900)
    assert r["ticket"] and w1.history()["items"]
    spoof = SlowClient(
        None, "r1", "w1", http=svc.http, token="tok-2"
    )  # w2 claiming to be w1
    for call in (
        spoof.history,
        lambda: spoof.status(r["ticket"]),
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
        with pytest.raises(ServiceError) as e:
            call()
        assert e.value.code == 403
    # a trusted client (the herd's Eval service, issue #10 3a) submits for any worker via manifest.actor
    trusted = SlowClient(None, "r1", "w2", http=svc.http, key=svc.core.system_key)
    assert trusted.submit_dir(
        stub_candidate(tmp_path / "e", ["locality"]), predicted=-900
    )["ticket"]
