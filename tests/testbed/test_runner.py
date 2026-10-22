"""tb run --arm herd (issue #10, 3c), end to end against a real HTTP service, with a stand-in herd."""

import json
import os
import subprocess
import sys

import pytest

from testbed.recorder.log import read_events


@pytest.mark.timeout(300)
def test_herd_arm_runs_the_herd_command(tmp_path):
    herd = (
        f"{sys.executable} -m testbed.arms.scripted.fake_herd --arm S2 --problem {{problem_dir}} "
        "--slow-url {slow_url} --run-id {run_id} --workers {workers} --hours {hours} "
        "--integrator-config {integrator_config}"
    )
    env = dict(os.environ, TB_STATE_DIR=str(tmp_path / "state"), TB_HERD_CMD=herd)
    env.pop("TB_SYSTEM_KEY", None)
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "testbed.cli.main",
            "run",
            "--arm",
            "herd",
            "--problem",
            "stub",
            "--workers",
            "2",
            "--hours",
            "0.05",
            "--run-id",
            "r-herd",
            "--overrides",
            json.dumps({"visible_delay_s": 1, "slow_seeds": 2}),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=280,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    wd = tmp_path / "state" / "runs" / "r-herd"
    events = read_events(wd / "events.jsonl")
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1 and finals[0]["actor"] == "runner"
    assert any(e["type"] == "final_check" for e in events)
    assert [e["actor"] for e in events if e["type"] == "submit"] == ["w1"]
    assert not any(
        e["type"] == "integrator_frozen" for e in events
    )  # no testbed integrator in the herd arm
    assert json.loads((wd / "integrator_config.json").read_text())["accept_se"] == 1.0
    assert (wd / "scorecard.txt").exists()
