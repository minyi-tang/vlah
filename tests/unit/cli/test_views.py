"""vlah status, events, search, report, inbox, stop and demo, read from a run's directory (#119)."""

import io
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.fixtures.herd import TOY_APPROACH, TOY_SOURCE
from tests.fixtures.runtime import (
    FakeIntegrator,
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    toy_candidate,
    toy_problem,
)
from vlah.adapters.human.cli.human import CliHuman
from vlah.cli import main
from vlah.core.eval.api import EvalRequest
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Stopped, event_from_json
from vlah.core.types.ids import ActorId
from vlah.ports.human import HumanMessage
from vlah.runtime.command import RUNS_DIRECTORY, write_report
from vlah.runtime.herd import HerdRun, build_herd, events_on
from vlah.runtime.status import read_status

pytestmark = pytest.mark.cli

WORKER = ActorId("worker-01")
RUN_ID = "r-1"
DEAD_END_CLAIM_ID = "c-worker-01-00001"
# The toy limit is 10 machine-hours; with the two judge runs to come this passes
# the 80% that escalates and stays short of the limit that stops the run.
EARLIER_SPEND_FRACTION = 0.75
STARTED_WAIT_STEPS = 1000
STARTED_WAIT_SECONDS = 0.01
FINISH_WAIT_SECONDS = 10.0


@dataclass(frozen=True, kw_only=True)
class FinishedRun:
    problem_root: Path
    run: HerdRun


@pytest.fixture(scope="module")
def finished_run(tmp_path_factory: pytest.TempPathFactory) -> FinishedRun:
    """A whole run on disk: a merge into baseline@1, a dead end, an escalation,
    then a stop, the closeout and report.json, as vlah run leaves them."""
    tmp_path = tmp_path_factory.mktemp("finished")
    problem_root = tmp_path / "problem"
    clock = FakeClock()
    run = build_herd(
        toy_problem(problem_root, worker_count=1),
        "S2",
        MemoryAssembly(
            clock=clock,
            root=tmp_path,
            use_real_eval=True,
            use_real_integrator=True,
            use_git_blackboard=True,
        ),
        RUN_ID,
        root=problem_root / RUNS_DIRECTORY,
        worker_count=1,
        duration_seconds=3600.0,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
    ).prepare()
    response = run.components.eval_service.request(
        EvalRequest(
            candidate_directory=toy_candidate(tmp_path / "candidate", WORKER, 2.0),
            predictions={TOY_SOURCE.id: 2.0},
            rationale="worker-01 raises every seed by two",
        ),
        WORKER,
    )
    assert response.claim_id is not None
    dead_end = Claim(
        id=DEAD_END_CLAIM_ID,
        type="dead_end",
        status="proposed",
        approach=TOY_APPROACH,
        author=WORKER,
        evidence=(EvidenceRef(claim=response.claim_id),),
        body="Batching jobs by deadline gives no gain.",
    )
    run.components.blackboard.write(
        WORKER,
        {f"claims/{WORKER}/{dead_end.id}.md": render_claim(dead_end).encode()},
        "dead end",
    )
    limit = run.context.problem.limits.eval_machine_hours
    run.components.budget.record(
        "worker", TOY_APPROACH, TOY_SOURCE.id, limit * EARLIER_SPEND_FRACTION
    )
    for _ in range(3):
        run.tick(clock.now_seconds())
        clock.advance(TOY_SOURCE.latency_seconds + 1.0)
    run.stop()
    write_report(run.report(), run.context.run_root / "report.json")
    return FinishedRun(problem_root=problem_root, run=run)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    ("arguments", "text_marker", "json_marker"),
    [
        (("status",), "worker-01  idle", '"agent":"worker-01"'),
        (("events",), "baseline@1 = baseline@0", '"type":"baseline_advanced"'),
        (
            ("events", "--follow", "--type", "stopped"),
            "stopped            human",
            '"reason":"human"',
        ),
        (("search", "deadline"), DEAD_END_CLAIM_ID, f'"{DEAD_END_CLAIM_ID}"'),
        (("report",), "deliverable: baseline@1", '"deliverable":{"k":1'),
        (("inbox",), "spend_over_fraction", '"target":"spend_over_fraction"'),
    ],
    ids=["status", "events", "events-follow", "search", "report", "inbox"],
)
def test_each_view_prints_from_a_finished_runs_directory(
    finished_run: FinishedRun,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    text_marker: str,
    json_marker: str,
    as_json: bool,
) -> None:
    """events --follow on a finished run must return, not wait for more."""
    command, *rest = arguments
    exit_code = main(
        [command, str(finished_run.problem_root), *rest]
        + (["--json"] if as_json else [])
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    if as_json:
        documents: list[object] = (
            [json.loads(line) for line in output.splitlines()]
            if command == "events"
            else [json.loads(output)]
        )
        assert json_marker in json.dumps(documents, separators=(",", ":"))
    else:
        assert text_marker in output


def test_events_are_the_runs_own_events_in_order(
    finished_run: FinishedRun, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reads the repository with git plumbing, not through the store: both
    readers must give the same events with the same sequence numbers."""
    assert main(["events", str(finished_run.problem_root), "--json"]) == 0
    printed = [event_from_json(line) for line in capsys.readouterr().out.splitlines()]
    expected = list(events_on(finished_run.run.components.blackboard))
    assert [(event.seq, event.local_id) for event in printed] == [
        (event.seq, event.local_id) for event in expected
    ]
    assert printed == expected


def test_vlah_stop_ends_a_running_run_with_one_closeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    problem_root = tmp_path / "problem"
    clock = FakeClock()
    run = build_herd(
        toy_problem(problem_root),
        "S2",
        MemoryAssembly(clock=clock, root=tmp_path),
        RUN_ID,
        root=problem_root / RUNS_DIRECTORY,
        worker_count=2,
        duration_seconds=3600.0,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
        poll_seconds=0.01,
    ).start()
    try:
        for _ in range(STARTED_WAIT_STEPS):
            if run.status_path.is_file():
                break
            else:
                time.sleep(STARTED_WAIT_SECONDS)
        assert main(["stop", str(problem_root)]) == 0
        assert run.wait(FINISH_WAIT_SECONDS), "the supervisor never picked up the stop"
    finally:
        run.stop()
    integrator = run.components.integrator
    assert isinstance(integrator, FakeIntegrator) and integrator.closeout_count == 1
    stopped = [
        event.data
        for event in events_on(run.components.blackboard)
        if isinstance(event.data, Stopped)
    ]
    assert [event.reason for event in stopped] == ["human"]
    assert read_status(run.status_path).phase == "finished"
    capsys.readouterr()
    assert main(["stop", str(problem_root)]) == 2
    assert "already finished" in capsys.readouterr().err


def test_vlah_demo_shows_wins_a_duplicate_and_a_dead_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["demo", "--seed", "1"]) == 0
    output = capsys.readouterr().out
    assert output.count("baseline_advanced") >= 2
    for marker in ("dedup_hit", "escalation", "1 duplicate(s) skipped", "1 dead end"):
        assert marker in output


def test_the_cli_human_reads_one_typed_line_per_reply() -> None:
    read_descriptor, write_descriptor = os.pipe()
    with (
        os.fdopen(read_descriptor, encoding="utf-8") as replies,
        os.fdopen(write_descriptor, "w", encoding="utf-8") as typed,
    ):
        human = CliHuman(
            output=io.StringIO(), replies=replies, clock=FakeClock(current_seconds=5.0)
        )
        assert human.receive(0.0) is None, "nothing typed yet"
        typed.write("continue\n")
        typed.flush()
        assert human.receive(FINISH_WAIT_SECONDS) == HumanMessage(
            sender="human", text="continue", timestamp_seconds=5.0
        )
