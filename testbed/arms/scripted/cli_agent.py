"""A deterministic stand-in for an LLM agent, for smoke-testing tb run end to end (problem A).

It only uses what a worker has: its workspace (problem/, work/, baseline/, PROMPT.md) and the tb
commands. It writes PLAN.md, edits work/policy.py (one idea per actor), checks it on the fast tier,
submits with a prediction, waits for the result, and declares it with tb final.

    tb run --arm isolated --problem sched --workers 2 --hours 0.1 \
        --agent-cmd "python -m testbed.arms.scripted.cli_agent" \
        --overrides '{"slow_seeds": 2, "visible_delay_s": 5}'
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IDEAS = {
    # actor index -> (direction, code appended to policy.py)
    0: (
        "backfill",
        """

def admit(job, state):
    # do not block the queue on a job that cannot fit anywhere right now
    return any(m.free_cores >= job.cores for m in state.machines)
""",
    ),
    1: (
        "est_order",
        """

def order(queue, state):
    old = sorted((j for j in queue if j.wait_so_far > 3600), key=lambda j: j.enqueue_t)
    young = sorted((j for j in queue if j.wait_so_far <= 3600), key=lambda j: j.est_dur)
    return [j.id for j in old + young]
""",
    ),
}
STATUS_POLL_SECONDS = 2


def tb(*tb_args):
    completed = subprocess.run(
        [sys.executable, "-m", "testbed.cli.main", *tb_args],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"tb {' '.join(tb_args)} failed: {completed.stderr or completed.stdout}"
        )
    return completed.stdout


def main():
    actor = os.environ.get("TB_ACTOR", "w1")
    actor_index = (int(actor[1:]) - 1) if actor[1:].isdigit() else 0
    direction, code = IDEAS[actor_index % len(IDEAS)]
    workspace = Path.cwd()
    (workspace / "PLAN.md").write_text(
        f"# PLAN ({actor})\n\nOne direction: {direction}. One slow submission.\n"
    )
    candidate_dir = workspace / "cand"
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)
    shutil.copytree(workspace / "baseline", candidate_dir)
    with (candidate_dir / "policy.py").open("a") as handle:
        handle.write(code)
    fast_path = workspace / "fast.json"
    tb("fast", str(candidate_dir), "--seed", "1", "--out", str(fast_path))
    fast_result = json.loads(fast_path.read_text())
    print(
        f"{actor}: fast {fast_result['score']} vs baseline {fast_result['baseline_score']}",
        flush=True,
    )
    if (
        fast_result["score"] is None
        or fast_result["score"] <= fast_result["baseline_score"]
    ):
        return
    receipt = json.loads(
        tb(
            "submit",
            str(candidate_dir),
            "--predict",
            str(fast_result["score"]),
            "--note",
            direction,
            "--direction",
            direction,
        )
    )
    ticket = receipt.get("of") or receipt["ticket"]
    while True:
        status = json.loads(tb("status", ticket))
        if "result" in status:
            print(
                f"{actor}: slow {status['result']['score']}"
                f" constraint_ok={status['result']['constraint_ok']}",
                flush=True,
            )
            break
        time.sleep(STATUS_POLL_SECONDS)
    tb("final", ticket)


if __name__ == "__main__":
    main()
