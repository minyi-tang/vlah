"""Stand-in for the herd's "vlah arm run" (issue #10, 3c), to test tb run --arm herd without the herd.

It checks the integrator config it was given, submits one stub candidate as a worker through a
trusted client (manifest.actor), waits for the result and calls the run-level final as the runner.
"""

import argparse
import os
import shutil
import tempfile
import time
from pathlib import Path

import yaml

from testbed.cli.client import SlowClient
from testbed.integrator.core import IntegratorConfig

STATUS_POLL_SECONDS = 0.5


def main():
    parser = argparse.ArgumentParser()
    for flag in (
        "--arm",
        "--problem",
        "--slow-url",
        "--run-id",
        "--workers",
        "--hours",
        "--integrator-config",
    ):
        parser.add_argument(flag, required=True)
    arguments = parser.parse_args()
    assert (
        IntegratorConfig.from_json(Path(arguments.integrator_config).read_text())
        == IntegratorConfig()
    )
    system_key = os.environ["TB_SYSTEM_KEY"]
    candidate_dir = Path(tempfile.mkdtemp(prefix="fake-herd-"))
    shutil.copytree(
        Path(arguments.problem) / "policy", candidate_dir, dirs_exist_ok=True
    )
    params = yaml.safe_load((candidate_dir / "params.yaml").read_text())
    params["ingredients"]["backfill"] = True
    (candidate_dir / "params.yaml").write_text(yaml.safe_dump(params))
    worker = SlowClient(arguments.slow_url, arguments.run_id, "w1", key=system_key)
    receipt = worker.submit_dir(
        candidate_dir, predicted=-900.0, target="baseline@0", direction="backfill"
    )
    ticket = receipt.get("of") or receipt["ticket"]
    while "result" not in worker.status(ticket):
        time.sleep(STATUS_POLL_SECONDS)
    SlowClient(arguments.slow_url, arguments.run_id, "runner", key=system_key).final(
        ticket=ticket
    )


if __name__ == "__main__":
    main()
