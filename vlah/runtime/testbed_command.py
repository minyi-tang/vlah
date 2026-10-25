"""The tb run herd-arm entry point; the enclosing testbed runner owns the scorecard."""

import math
import os
import re
import sys
from pathlib import Path
from random import Random
from typing import TextIO
from urllib.parse import urlsplit

from testbed.integrator.core import IntegratorConfig
from vlah.adapters.signal.testbed_http.client import SlowTierClient
from vlah.core.types.clock import SystemClock
from vlah.runtime.api import ArmRunRequest, Report
from vlah.runtime.command import (
    RUNS_DIRECTORY,
    SUPERVISOR_POLL_SECONDS,
    RunRequest,
    report_summary,
    run_record,
    wait_for,
    write_report,
)
from vlah.runtime.herd import build_herd
from vlah.runtime.testbed_assembly import TestbedAssembly
from vlah.runtime.testbed_problem import convert_testbed_problem

DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "examples" / "testbed-sched"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def run_testbed_arm(
    request: ArmRunRequest,
    integrator_config_path: Path,
    template_root: Path,
    stream: TextIO = sys.stdout,
) -> Report:
    if request.worker_count < 1:
        raise ValueError("--workers must be positive")
    elif request.duration_hours <= 0 or not math.isfinite(request.duration_hours):
        raise ValueError("--hours must be finite and positive")
    elif RUN_ID_PATTERN.fullmatch(request.run_id) is None:
        raise ValueError("--run-id must be a safe directory name")
    else:
        pass
    url = urlsplit(request.slow_eval_url)
    if (
        url.scheme not in ("http", "https")
        or url.hostname is None
        or url.username
        or url.password
    ):
        raise ValueError(
            "--slow-url must be an HTTP(S) service URL without credentials"
        )
    else:
        pass
    system_key = os.environ.get("TB_SYSTEM_KEY")
    if not system_key:
        raise ValueError("TB_SYSTEM_KEY is required; only the arm runner receives it")
    else:
        pass
    try:
        integrator_config = IntegratorConfig.from_json(
            integrator_config_path.read_text(encoding="utf-8")
        )
    except TypeError as error:
        raise ValueError(
            f"{integrator_config_path}: invalid IntegratorConfig: {error}"
        ) from error

    clock = SystemClock()
    problem = convert_testbed_problem(
        request.problem_root,
        template_root,
        request.problem_root.resolve().parent / "herd-problem",
        clock=clock,
    )
    runs_root = problem.root / RUNS_DIRECTORY
    record = run_record(
        RunRequest(
            problem_root=problem.root,
            arm=request.arm,
            worker_count=request.worker_count,
            duration_hours=request.duration_hours,
            run_id=request.run_id,
        ),
        runs_root,
        clock,
    )
    assembly = TestbedAssembly(
        client=SlowTierClient(
            base_url=request.slow_eval_url, run_id=request.run_id, system_key=system_key
        ),
        integrator_config=integrator_config,
        worker_count=request.worker_count,
    )
    try:
        herd = build_herd(
            problem,
            record.arm,
            assembly,
            record.run_id,
            root=runs_root,
            worker_count=record.worker_count,
            duration_seconds=record.duration_seconds,
            clock=clock,
            random=Random(record.seed),
            poll_seconds=SUPERVISOR_POLL_SECONDS,
            started_seconds=record.started_seconds,
        )
        print(
            f"[{record.run_id}] starting {record.worker_count} testbed workers under "
            f"{herd.context.run_root}",
            file=stream,
        )
        run = herd.start()
        try:
            wait_for(run, stream)
            report = run.report()
            final = assembly.finalize(report)
            (herd.context.run_root / "final.json").write_text(
                final.model_dump_json(indent=2), encoding="utf-8"
            )
            write_report(report, herd.context.run_root / "report.json")
        finally:
            if run.status().is_running:
                run.stop()
            else:
                pass
    finally:
        assembly.close()
    print(report_summary(report), file=stream)
    print(f"testbed final: {final.ticket} ({final.hash})", file=stream)
    return report
