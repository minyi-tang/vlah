"""The command judge: a shell command as a SignalSource, and baseline scores from it."""

import json
import time
from pathlib import Path

import pytest

from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.signal.command.judge import (
    CommandBaselineScores,
    CommandJudge,
    parse_judge_output,
)
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import ScoreResult, SourceBudget, SourceDecl
from vlah.ports.signal_source import CandidateRef

pytestmark = pytest.mark.runtime

WORKER = ActorId("worker-01")
RUBRIC = SourceDecl(
    id="rubric",
    mode="pull",
    latency="1m",
    budget=SourceBudget(per_day=100, allocated_by="human"),
    cost_machine_hours=0.05,
    trust=0.9,
    readable_by=("critic",),
)
JUDGE_SCRIPT = """\
import json, os, sys
from pathlib import Path
candidate = Path(sys.argv[1])
score = float((candidate / "score.txt").read_text())
dataset = sys.argv[3]
if score < 0:
    sys.exit(3)
print("a log line the judge prints first")
print(json.dumps({
    "score": score,
    "per_seed": [score - 0.1, score + 0.1],
    "constraint_ok": score < 0.9,
    "dataset": dataset,
    "problem_root": os.environ["VLAH_PROBLEM_ROOT"],
    "hash": os.environ["VLAH_CANDIDATE_HASH"],
}))
"""
POLL_TIMEOUT_SECONDS = 20.0


def candidate_at(directory: Path, score: float, name: str = "c1") -> CandidateRef:
    payload = directory / name
    payload.mkdir(parents=True)
    (payload / "score.txt").write_text(str(score), encoding="utf-8")
    return CandidateRef(
        candidate_hash=f"{name}-hash",
        payload_directory=payload,
        manifest=CandidateManifest(
            kind="patch",
            target="baseline@0",
            approach="a-01",
            author=WORKER,
            created_at=0.0,
        ),
    )


def make_judge(tmp_path: Path) -> CommandJudge:
    problem_root = tmp_path / "problem"
    problem_root.mkdir()
    (problem_root / "judge.py").write_text(JUDGE_SCRIPT, encoding="utf-8")
    return CommandJudge(
        source_declaration=RUBRIC,
        command="python3 {problem_root}/judge.py {candidate_dir} --dataset {dataset}",
        problem_root=problem_root,
        clock=FakeClock(),
        log_directory=tmp_path / "judges",
    )


def wait_for(judge: CommandJudge, job_id: str) -> ScoreResult:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = judge.poll(job_id)
        if result is not None:
            return result
        else:
            time.sleep(0.05)
    raise AssertionError(f"{job_id} never finished")


def test_the_command_runs_with_the_placeholders_and_its_last_line_is_the_score(
    tmp_path: Path,
) -> None:
    judge = make_judge(tmp_path)
    candidate = candidate_at(tmp_path, 0.6)
    job_id = judge.submit(candidate, WORKER, "baseline@0", "audit")
    result = wait_for(judge, job_id)
    assert result.value == 0.6
    assert result.per_seed == (0.5, 0.7) and result.n == 2
    assert result.constraint_ok is True and result.is_cached is False
    assert result.extra["dataset"] == "audit"
    assert result.extra["problem_root"] == str(tmp_path / "problem")
    assert result.extra["hash"] == "c1-hash"
    assert (tmp_path / "judges" / "rubric-audit-c1-hash.out").exists()


def test_a_repeat_on_the_same_dataset_is_answered_from_the_first_run(
    tmp_path: Path,
) -> None:
    judge = make_judge(tmp_path)
    candidate = candidate_at(tmp_path, 0.4)
    first = judge.submit(candidate, WORKER, "baseline@0", "dev")
    repeat = judge.submit(candidate, ActorId("worker-02"), "baseline@0", "dev")
    other_set = judge.submit(candidate, WORKER, "baseline@0", "audit")
    assert wait_for(judge, first).is_cached is False
    assert wait_for(judge, repeat).is_cached is True
    assert wait_for(judge, other_set).is_cached is False, "a dataset is its own run"
    assert judge.job_count == 3


def test_a_failing_or_silent_judge_is_a_void_run(tmp_path: Path) -> None:
    judge = make_judge(tmp_path)
    failing = candidate_at(tmp_path, -1.0, name="bad")
    result = wait_for(judge, judge.submit(failing, WORKER, "baseline@0", "dev"))
    assert result.value is None
    assert "exited with 3" in str(result.extra["error"])
    assert parse_judge_output("").value is None
    assert parse_judge_output("not json").extra["error"]
    assert parse_judge_output('{"score": 0.5}').per_seed == (0.5,)


def test_baseline_scores_measure_baseline_zero_once_and_read_later_records(
    tmp_path: Path,
) -> None:
    judge = make_judge(tmp_path)
    baseline = tmp_path / "baseline0"
    baseline.mkdir()
    (baseline / "score.txt").write_text("0.2", encoding="utf-8")
    clock = FakeClock()
    blackboard = make_store_blackboard(clock, name="judge")
    blackboard.write(ActorId("human"), {"spec.md": b"# Spec\n"}, "spec")
    scores = CommandBaselineScores(
        judges={"rubric": judge}, baseline_directory=baseline, blackboard=blackboard
    )
    assert scores("rubric", "baseline@0", "dev") == pytest.approx((0.1, 0.3))
    assert scores("rubric", "baseline@0", "dev") == pytest.approx((0.1, 0.3))
    assert judge.job_count == 0, "calibration runs outside the queue"
    assert scores("rubric", "baseline@1", "dev") is None
    record = BaselineRecord(
        k=1, parent=0, candidates=("c1-hash",), per_seed=(0.5, 0.7), created_ts=1.0
    )
    blackboard.write(
        ActorId("integrator"),
        {"baseline/1.json": record.model_dump_json().encode()},
        "baseline@1",
    )
    assert scores("rubric", "baseline@1", "dev") == (0.5, 0.7)
    assert scores("rubric", "baseline@1", "audit") is None
    assert (
        json.loads(
            (tmp_path / "judges" / "rubric-dev-baseline.out")
            .read_text()
            .splitlines()[-1]
        )["score"]
        == 0.2
    )


def test_a_repeat_after_a_void_run_runs_the_command_again(tmp_path: Path) -> None:
    """A void run is a judge failure, not a result: answering a resubmission from it
    would make every retry void too (first run of the model-graded judge)."""
    judge = make_judge(tmp_path)
    candidate = candidate_at(tmp_path, -1.0, name="flaky")
    first = wait_for(judge, judge.submit(candidate, WORKER, "baseline@0", "audit"))
    assert first.value is None
    (candidate.payload_directory / "score.txt").write_text("0.7", encoding="utf-8")
    again = wait_for(judge, judge.submit(candidate, WORKER, "baseline@0", "audit"))
    assert again.value == 0.7 and again.is_cached is False
    repeat = wait_for(judge, judge.submit(candidate, WORKER, "baseline@0", "audit"))
    assert repeat.value == 0.7 and repeat.is_cached is True
