"""Anti-cheat rule 1 on one machine: what an agent can and cannot read from inside its sandbox (macOS),
and that tb run refuses to start an LLM arm without one unless --unsafe-no-sandbox is given.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from testbed.arms.runner import agent_env, agent_sandbox_prefix
from testbed.cli.main import main as tb_main
from testbed.cli.pack import pack_problem
from testbed.problem_api import REPO_ROOT, STATE_DIR
from testbed.recorder.log import read_events

macos_sandbox_only = pytest.mark.skipif(
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
@macos_sandbox_only
def test_agent_cannot_read(sandbox, what, cmd):
    ws, run = sandbox
    cmd = cmd.replace("~", str(Path(os.path.expanduser("~"))))
    code, out = run(cmd)
    assert code != 0, f"{what} readable: {out[:200]}"


@macos_sandbox_only
@pytest.mark.timeout(300)
def test_agent_can_work(sandbox):
    ws, run = sandbox
    code, out = run(f"ls work && tb fast {ws}/work --seed 1")
    assert code == 0, out[-2000:]
    assert "score" in out


class ServiceStarted(Exception):
    """Raised by the stubbed slow-eval service: the sandbox check let the run go on."""


def stub_service(data_dir: Path) -> str:
    raise ServiceStarted(data_dir)


@pytest.mark.parametrize(
    "platform,has_sandbox_exec,refuses",
    [("linux", True, True), ("darwin", False, True), ("darwin", True, False)],
    ids=["not-macos", "no-sandbox-exec", "macos-sandboxed"],
)
def test_run_checks_the_sandbox_before_anything_starts(
    tmp_path, monkeypatch, platform, has_sandbox_exec, refuses
):
    """Bug: on Linux, or on macOS without sandbox-exec, tb run started the agents with no sandbox and
    full read access to hidden material, without a word (#86). On macOS with sandbox-exec the run
    must still go on without the flag.
    """
    real_which = shutil.which

    def which(name, *rest, **kw):
        if name != "sandbox-exec":
            return real_which(name, *rest, **kw)
        elif has_sandbox_exec:
            return (
                "/usr/bin/sandbox-exec"  # never executed: the service stub raises first
            )
        else:
            return None

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.delenv("TB_SLOW_URL", raising=False)
    monkeypatch.setattr("testbed.arms.runner.start_local_service", stub_service)
    workdir = tmp_path / "wd"
    with pytest.raises(SystemExit if refuses else ServiceStarted) as outcome:
        tb_main(
            [
                "run",
                "--arm",
                "isolated",
                "--problem",
                "stub",
                "--agent-cmd",
                "true",
                "--workdir",
                str(workdir),
            ]
        )
    if refuses:
        assert "--unsafe-no-sandbox" in str(outcome.value)
        assert not workdir.exists()
    else:
        pass


@pytest.mark.timeout(300)
def test_run_with_unsafe_flag_proceeds_and_records_it(tmp_path):
    """With --unsafe-no-sandbox the arm runs, the agent really is unsandboxed (it can list the slow-eval
    data, which the macOS sandbox denies), and the run's events say so.
    """
    state = tmp_path / "state"
    env = dict(os.environ, TB_STATE_DIR=str(state))
    env.pop("TB_SYSTEM_KEY", None)
    env.pop("TB_SLOW_URL", None)
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "testbed.cli.main",
            "run",
            "--arm",
            "single",
            "--problem",
            "stub",
            "--hours",
            "0.005",
            "--run-id",
            "r-unsafe",
            "--agent-cmd",
            f"/bin/ls {state}/slow_eval > {{workdir}}/listing.txt",
            "--unsafe-no-sandbox",
            "--overrides",
            json.dumps({"visible_delay_s": 1, "slow_seeds": 2}),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=280,
    )
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    wd = state / "runs" / "r-unsafe"
    events = read_events(wd / "events.jsonl")
    unsafe = [e for e in events if e["type"] == "unsafe_no_sandbox"]
    assert len(unsafe) == 1 and unsafe[0]["actor"] == "runner"
    assert unsafe[0]["data"]["actor"] == "all"
    assert unsafe[0]["data"]["reason"] == (
        "flag" if sys.platform == "darwin" else "not-macos"
    )
    assert "system.key" in (wd / "agents" / "solo" / "listing.txt").read_text()
