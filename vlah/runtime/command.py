"""vlah run: one arm of a herd on a problem folder, on this machine (quickstart section 6).

Run state lives under <problem>/.herd/: runs/<run id>/ holds the blackboard
repository, the Integrator's baselines and the judges' output, and is protected
from the workers; sandboxes/<run id>/ and logs/<run id>/ are theirs to write.
Ctrl-C stops the run cleanly: the workers are killed and the Integrator closes out,
so everything learned so far is kept.
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
from vlah.core.types.clock import SystemClock
from vlah.runtime.api import Report
from vlah.runtime.herd import HerdRun, build_herd
from vlah.runtime.local_assembly import LocalAssembly

logger = logging.getLogger(__name__)

RUNS_DIRECTORY = ".herd/runs"
STATUS_EVERY_SECONDS = 30.0
SECONDS_PER_HOUR = 3600.0
SUPERVISOR_POLL_SECONDS = 5.0


@dataclass(frozen=True, kw_only=True)
class RunRequest:
    problem_root: Path
    arm: ArmName
    worker_count: int
    duration_hours: float
    run_id: str | None = None
    seed: int = 0
    allow_unenforced_egress: bool = False
    runs_root: Path | None = None


def default_run_id(arm: ArmName) -> str:
    return f"r-{time.strftime('%Y%m%d-%H%M%S')}-{arm}"


def report_summary(report: Report) -> str:
    deliverable = report.deliverable
    lines = [
        f"run {report.run_id} ({report.arm}) finished",
        f"deliverable: baseline@{deliverable.k} with {len(deliverable.candidates)} "
        f"merged candidate(s)",
        f"baselines written: {len(report.baselines)}",
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
    run_id = request.run_id or default_run_id(request.arm)
    runs_root = request.runs_root or problem.root / RUNS_DIRECTORY
    assembly = LocalAssembly(allow_unenforced_egress=request.allow_unenforced_egress)
    herd = build_herd(
        problem,
        request.arm,
        assembly,
        run_id,
        root=runs_root,
        worker_count=request.worker_count,
        duration_seconds=request.duration_hours * SECONDS_PER_HOUR,
        clock=SystemClock(),
        random=Random(request.seed),
        poll_seconds=SUPERVISOR_POLL_SECONDS,
    )
    print(
        f"[{run_id}] starting {request.worker_count} worker(s) for "
        f"{request.duration_hours:g} h under {runs_root / run_id}",
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
