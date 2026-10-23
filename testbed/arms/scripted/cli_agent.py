"""A deterministic stand-in for an LLM agent, for smoke-testing `tb run` end to end (problem A).

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


def tb(*args):
    r = subprocess.run(
        [sys.executable, "-m", "testbed.cli.main", *args],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"tb {' '.join(args)} failed: {r.stderr or r.stdout}")
    return r.stdout


def main():
    actor = os.environ.get("TB_ACTOR", "w1")
    idx = (int(actor[1:]) - 1) if actor[1:].isdigit() else 0
    direction, code = IDEAS[idx % len(IDEAS)]
    ws = Path.cwd()
    (ws / "PLAN.md").write_text(
        f"# PLAN ({actor})\n\nOne direction: {direction}. One slow submission.\n"
    )
    cand = ws / "cand"
    if cand.exists():
        shutil.rmtree(cand)
    shutil.copytree(ws / "baseline", cand)
    with (cand / "policy.py").open("a") as f:
        f.write(code)
    out = ws / "fast.json"
    tb("fast", str(cand), "--seed", "1", "--out", str(out))
    fast = json.loads(out.read_text())
    print(
        f"{actor}: fast {fast['score']} vs baseline {fast['baseline_score']}",
        flush=True,
    )
    if fast["score"] is None or fast["score"] <= fast["baseline_score"]:
        return
    r = json.loads(
        tb(
            "submit",
            str(cand),
            "--predict",
            str(fast["score"]),
            "--note",
            direction,
            "--direction",
            direction,
        )
    )
    ticket = r.get("of") or r["ticket"]
    while True:
        st = json.loads(tb("status", ticket))
        if "result" in st:
            print(
                f"{actor}: slow {st['result']['score']} constraint_ok={st['result']['constraint_ok']}",
                flush=True,
            )
            break
        time.sleep(2)
    tb("final", ticket)


if __name__ == "__main__":
    main()
