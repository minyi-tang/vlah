"""Anti-cheat rule 1 on one machine: what an agent can and cannot read from inside its sandbox (macOS)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from testbed.arms.runner import agent_env, agent_sandbox_prefix
from testbed.cli.pack import pack_problem
from testbed.problem_api import REPO_ROOT, STATE_DIR

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="macOS sandbox"
)


@pytest.fixture(scope="module")
def sandbox():
    # the workspace must live outside the user's TMPDIR, which the sandbox denies
    ws = STATE_DIR / "sbx-test" / "ws"
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    pack_problem("sched", ws / "problem")
    shutil.copytree(REPO_ROOT / "problems" / "sched" / "policy", ws / "work")
    (STATE_DIR / "slow_eval").mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "slow_eval" / "probe.txt").write_text("secret")
    for d in (
        REPO_ROOT / "private" / "testbed",
        Path.home() / ".vlah-private" / "testbed",
    ):
        d.mkdir(parents=True, exist_ok=True)
        (d / "probe.txt").write_text("secret")
    env = agent_env(
        {k: v for k, v in os.environ.items() if not k.startswith("TB_")}, ws, "w1"
    )
    env.update(
        TB_PROBLEM_DIR=str(ws / "problem"),
        TB_SANDBOX="inherit",
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    prefix = agent_sandbox_prefix(ws, [])
    assert prefix

    def run(cmd):
        r = subprocess.run(
            prefix + ["/bin/sh", "-c", cmd],
            cwd=ws,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return r.returncode, r.stdout + r.stderr

    return ws, run


@pytest.mark.parametrize(
    "what,cmd",
    [
        ("spec documents", "cat ~/Documents/*.md"),
        ("repo docs", f"cat {REPO_ROOT}/testbed/DECISIONS.md"),
        ("problem source", f"cat {REPO_ROOT}/problems/sched/problem.py"),
        ("hidden material (repo)", f"cat {REPO_ROOT}/private/testbed/probe.txt"),
        (
            "hidden material (home)",
            f"cat {Path.home()}/.vlah-private/testbed/probe.txt",
        ),
        ("slow-eval data", f"cat {STATE_DIR}/slow_eval/probe.txt"),
        (
            "alternative policies",
            f"cat {REPO_ROOT}/testbed/extras/alt_policies/sjf_pure.py",
        ),
        ("extras code", "python -c 'import testbed.extras.checks'"),
        ("session transcripts", "ls /Users/*/.copilot/session-state"),
        ("system temp dir", f"ls {os.environ.get('TMPDIR', '/tmp')}"),
    ],
)
def test_agent_cannot_read(sandbox, what, cmd):
    ws, run = sandbox
    cmd = cmd.replace("~", str(Path(os.path.expanduser("~"))))
    code, out = run(cmd)
    assert code != 0, f"{what} readable: {out[:200]}"


@pytest.mark.timeout(300)
def test_agent_can_work(sandbox):
    ws, run = sandbox
    code, out = run(f"ls work && tb fast {ws}/work --seed 1")
    assert code == 0, out[-2000:]
    assert "score" in out
