"""Issue #106: one sqlite connection shared by the request threads, the publisher and the
evaluation pool must serialize every statement, and a ticket whose evaluation fails before the
evaluation starts must end as failed, not stay running forever."""

import threading

import pytest

from testbed.slow_eval.testing import InProcessService
from tests.testbed.helpers import stub_candidate

THREADS = 8
CALLS_PER_THREAD = 60


@pytest.fixture
def service(tmp_path):
    in_process = InProcessService(tmp_path)
    yield in_process
    in_process.close()


def test_concurrent_reads_and_submits_share_one_connection_safely(service, tmp_path):
    service.new_run(quota_per_hour=1000, visible_delay_s=0)
    core = service.core
    errors: list[BaseException] = []

    def reader():
        try:
            for _ in range(CALLS_PER_THREAD):
                core.history("r1", "w1")
                core.events("r1")
                core.meta("r1")
        except BaseException as error:  # the race shows up as sqlite3.InterfaceError
            errors.append(error)

    def submitter(index: int):
        try:
            worker = service.client("r1", f"w{index}")
            for call in range(3):
                worker.submit_dir(
                    stub_candidate(tmp_path / f"c{index}-{call}", ["backfill"]),
                    predicted=-900,
                )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=reader) for _ in range(THREADS)]
    threads += [threading.Thread(target=submitter, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors[:3]
    assert all(not thread.is_alive() for thread in threads)


def test_a_ticket_that_fails_before_evaluation_ends_as_failed(service, tmp_path):
    service.new_run(visible_delay_s=0)
    worker = service.client("r1", "w1")
    # The symptom of #106: the run's calibration read back as NULL, so json.loads raised before
    # the evaluation's try block and the ticket stayed 'running' forever.
    service.core.db.execute("UPDATE runs SET cal=NULL WHERE run_id='r1'")
    receipt = worker.submit_dir(
        stub_candidate(tmp_path / "c", ["backfill"]), predicted=-900
    )
    status = worker.status(receipt["ticket"])
    assert status["state"] == "failed"
    assert "TypeError" in status["result"]["extra"]["error"]
