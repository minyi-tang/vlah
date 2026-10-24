"""vlah run: one arm of a herd on a problem folder, on this machine (quickstart section 6).

Run state lives under <problem>/.herd/: runs/<run id>/ holds the blackboard
repository, the key-value store, the Integrator's baselines and the judges' output,
and is protected from the workers; sandboxes/<run id>/ and logs/<run id>/ are theirs
to write. Ctrl-C stops the run cleanly: the workers are killed and the Integrator
closes out, so everything learned so far is kept.

A new run writes runs/<run id>/run.json first: its arm, worker count, seed, length
and start time. vlah run --resume <run id> reads it back and continues a stopped or
crashed run with the same settings until its original deadline.
"""

import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import TextIO

from vlah.arms.config import ArmName
from vlah.core.problem.loader import load_problem
from vlah.core.types.base import FrozenModel
from vlah.core.types.clock import Clock, SystemClock
from vlah.runtime.api import Report
from vlah.runtime.herd import HerdRun, build_herd
from vlah.runtime.local_assembly import LocalAssembly

logger = logging.getLogger(__name__)

RUNS_DIRECTORY = ".herd/runs"
STATUS_EVERY_SECONDS = 30.0
SECONDS_PER_HOUR = 3600.0
SUPERVISOR_POLL_SECONDS = 5.0
RUN_RECORD_FILE_NAME = "run.json"


@dataclass(frozen=True, kw_only=True)
class RunRequest:
    """is_resume continues run_id with its own settings; arm, worker_count,
    duration_hours and seed then come from its run.json."""

    problem_root: Path
    arm: ArmName
    worker_count: int
    duration_hours: float
    run_id: str | None = None
    seed: int = 0
    allow_unenforced_egress: bool = False
    runs_root: Path | None = None
    is_resume: bool = False


class RunRecord(FrozenModel):
    """runs/<run id>/run.json: how the run began, so a resumed run continues it."""

    run_id: str
    arm: ArmName
    worker_count: int
    duration_seconds: float
    started_seconds: float
    seed: int


def default_run_id(arm: ArmName) -> str:
    return f"r-{time.strftime('%Y%m%d-%H%M%S')}-{arm}"


def run_record(request: RunRequest, runs_root: Path, clock: Clock) -> RunRecord:
    """A resumed run's record, read back; a new run's, written before it starts."""
    if request.is_resume:
        if request.run_id is None:
            raise ValueError("resuming a run needs its run id")
        else:
            path = runs_root / request.run_id / RUN_RECORD_FILE_NAME
            if not path.is_file():
                raise ValueError(
                    f"no run {request.run_id} to resume: {path} is missing"
                )
            else:
                return RunRecord.model_validate_json(path.read_bytes())
    else:
        run_id = request.run_id or default_run_id(request.arm)
        run_root = runs_root / run_id
        if run_root.exists():
            raise ValueError(
                f"{run_root} already exists; continue that run with --resume {run_id}"
            )
        else:
            record = RunRecord(
                run_id=run_id,
                arm=request.arm,
                worker_count=request.worker_count,
                duration_seconds=request.duration_hours * SECONDS_PER_HOUR,
                started_seconds=clock.now_seconds(),
                seed=request.seed,
            )
            run_root.mkdir(parents=True)
            (run_root / RUN_RECORD_FILE_NAME).write_text(
                record.model_dump_json(indent=2), encoding="utf-8"
            )
            return record


def report_summary(report: Report) -> str:
    deliverable = report.deliverable
    lines = [
        f"run {report.run_id} ({report.arm}) finished",
        f"deliverable: baseline@{deliverable.k} with {len(deliverable.candidates)} "
        f"merged candidate(s)",
        f"baselines written: {len(report.baselines)}",
        f"holdout score (verdict split): {report.metrics.holdout_score}",
    ]
    for candidate in deliverable.candidates:
        lines.append(f"  merged {candidate}")
    return "\n".join(lines)


def write_report(report: Report, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": report.run_id,
                "arm": report.arm,
                "deliverable": report.deliverable.model_dump(mode="json"),
                "holdout_score": report.metrics.holdout_score,
                "baselines": [
                    record.model_dump(mode="json") for record in report.baselines
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def wait_for(run: HerdRun, stream: TextIO) -> None:
    """Block until the run stops on its own; Ctrl-C stops it cleanly."""
    try:
        while not run.wait(STATUS_EVERY_SECONDS):
            status = run.status()
            print(
                f"[{status.run_id}] baseline@{status.current_baseline}, "
                f"{status.judge_runs_used} judge runs, "
                f"{status.head_sequence} events",
                file=stream,
            )
    except KeyboardInterrupt:
        print(
            f"[{run.context.run_id}] stopping: closing out the Integrator", file=stream
        )
        run.stop()


def run_problem(request: RunRequest, stream: TextIO = sys.stdout) -> Report:
    problem = load_problem(request.problem_root)
    if problem.status != "aligned":
        raise ValueError(
            f"{problem.root}: charter/status.md is {problem.status}; sign the "
            "charter first (vlah sign)"
        )
    elif not problem.params.slots_per_approach:
        raise ValueError(
            f"{problem.root}: charter/params.yaml: slots_per_approach is empty, so "
            "no worker would get a task; give each approach its slot count"
        )
    else:
        pass
    runs_root = request.runs_root or problem.root / RUNS_DIRECTORY
    clock = SystemClock()
    record = run_record(request, runs_root, clock)
    run_id = record.run_id
    assembly = LocalAssembly(allow_unenforced_egress=request.allow_unenforced_egress)
    herd = build_herd(
        problem,
        record.arm,
        assembly,
        run_id,
        root=runs_root,
        worker_count=record.worker_count,
        duration_seconds=record.duration_seconds,
        clock=clock,
        random=Random(record.seed),
        poll_seconds=SUPERVISOR_POLL_SECONDS,
        started_seconds=record.started_seconds,
    )
    hours_left = max(
        0.0,
        record.started_seconds + record.duration_seconds - clock.now_seconds(),
    )
    print(
        f"[{run_id}] {'resuming' if request.is_resume else 'starting'} "
        f"{record.worker_count} worker(s) of arm {record.arm} with "
        f"{hours_left / SECONDS_PER_HOUR:.2f} h left under {runs_root / run_id}",
        file=stream,
    )
    run = herd.start()
    try:
        wait_for(run, stream)
        report = run.report()
    finally:
        assembly.close()
    write_report(report, runs_root / run_id / "report.json")
    print(report_summary(report), file=stream)
    return report
