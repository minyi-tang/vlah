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


def main():
    ap = argparse.ArgumentParser()
    for flag in (
        "--arm",
        "--problem",
        "--slow-url",
        "--run-id",
        "--workers",
        "--hours",
        "--integrator-config",
    ):
        ap.add_argument(flag, required=True)
    a = ap.parse_args()
    assert (
        IntegratorConfig.from_json(Path(a.integrator_config).read_text())
        == IntegratorConfig()
    )
    key = os.environ["TB_SYSTEM_KEY"]
    candidate = Path(tempfile.mkdtemp(prefix="fake-herd-"))
    shutil.copytree(Path(a.problem) / "policy", candidate, dirs_exist_ok=True)
    params = yaml.safe_load((candidate / "params.yaml").read_text())
    params["ingredients"]["backfill"] = True
    (candidate / "params.yaml").write_text(yaml.safe_dump(params))
    worker = SlowClient(a.slow_url, a.run_id, "w1", key=key)
    receipt = worker.submit_dir(
        candidate, predicted=-900.0, target="baseline@0", direction="backfill"
    )
    ticket = receipt.get("of") or receipt["ticket"]
    while "result" not in worker.status(ticket):
        time.sleep(0.5)
    SlowClient(a.slow_url, a.run_id, "runner", key=key).final(ticket=ticket)


if __name__ == "__main__":
    main()
