"""Installed-artifact probes, launched with isolated Python outside the checkout."""

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import testbed.integrator.core
from testbed.arms.runner import agent_env, agent_sandbox_prefix, agent_tmp
from testbed.cli.pack import pack_problem
from testbed.problem_api import (
    PROBLEMS_ROOT,
    STATE_DIR,
    hidden_root,
    load_problem,
    private_dirs,
    resolve_problem_dir,
)

CLI_PATH = Path(sys.executable).with_name("tb")
COMMAND_TIMEOUT_SECONDS = 180


def run_cli(arguments: list[str], environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        [str(CLI_PATH), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


def check_cli() -> None:
    """The CLI must load resources and edit workspace copies without writing into its package."""
    assert Path(testbed.integrator.core.__file__).is_relative_to(Path(sys.prefix))
    assert "pack-problem" in run_cli(["--help"])
    problem = load_problem("sched")
    assert hidden_root("sched") is None
    assert problem.public_seeds == [1, 2, 3, 4, 5]
    assert problem.evaluate(problem.baseline_dir(), "fast", 1)["score"] < 0
    source = load_problem("stub").baseline_dir() / "params.yaml"
    source_mode = stat.S_IMODE(source.stat().st_mode)
    assert not source_mode & stat.S_IWUSR
    packed = Path("stub-worker").resolve()
    run_cli(["pack-problem", "stub", str(packed)])
    params_path = packed / "policy/params.yaml"
    params_path.write_bytes(params_path.read_bytes())
    assert stat.S_IMODE(source.stat().st_mode) == source_mode
    assert not (packed / "pi_prompt.md").exists()
    result_path = Path("fast.json")
    run_cli(["fast", str(packed / "policy"), "--seed", "1", "--out", str(result_path)])
    result = json.loads(result_path.read_text())
    assert result["score"] == result["baseline_score"] == -1000.0
    assert result["per_item"] == [{"id": "total", "value": -1000.0}]
    assert resolve_problem_dir("stub-worker") == packed
    assert "SELFTEST PASS" in run_cli(["selftest", "--out", "selftest"])
    assert STATE_DIR == Path.home() / ".vlah-private/testbed/var"
    assert (STATE_DIR / "stub/five-ingredients/params.yaml").is_file()
    assert private_dirs() == []
    extras = subprocess.run(
        [str(CLI_PATH), "extras"],
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    assert extras.returncode != 0
    assert "requires a source checkout" in extras.stderr


def check_paths() -> None:
    """Explicit state/private roots must win without adding a site-packages fallback."""
    assert STATE_DIR == Path(os.environ["TB_STATE_DIR"])
    assert private_dirs() == [
        Path(os.environ["TB_PRIVATE"]),
        Path.home() / ".vlah-private/testbed",
    ]
    assert load_problem("stub").reference_dir().is_relative_to(STATE_DIR)


def check_sandbox() -> None:
    """A readable environment must not expose operator resources or private/run directories."""
    workspace = Path("sandbox-worker").resolve()
    pack_problem("stub", workspace / "problem")
    operator_paths = [
        PROBLEMS_ROOT / "sched/problem.py",
        PROBLEMS_ROOT / "sched/sites/common.yaml",
        PROBLEMS_ROOT / "stub/stub.yaml",
    ]
    for root in [STATE_DIR, Path(os.environ["TB_PRIVATE"])]:
        root.mkdir(parents=True, exist_ok=True)
        private_file = root / "operator-only.txt"
        private_file.write_text("not readable by workers")
        operator_paths.append(private_file)
    environment = agent_env(dict(os.environ), workspace, "worker")
    environment.update(TB_PROBLEM_DIR=str(workspace / "problem"), TB_SANDBOX="inherit")
    prefix = agent_sandbox_prefix(workspace)
    try:
        for source in operator_paths:
            denied = subprocess.run(
                prefix
                + [
                    sys.executable,
                    "-I",
                    "-c",
                    f"from pathlib import Path; Path({str(source)!r}).read_text()",
                ],
                cwd=workspace,
                env=environment,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
            assert denied.returncode != 0, source
        worker = subprocess.run(
            prefix
            + [
                str(CLI_PATH),
                "fast",
                str(workspace / "problem/policy"),
                "--seed",
                "1",
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        assert worker.returncode == 0, worker.stdout + worker.stderr
        assert "score -1000.0" in worker.stdout
    finally:
        shutil.rmtree(agent_tmp(workspace))


def check_herd() -> None:
    """An empty herd launch must run and submit its fallback from writable run state."""
    workdir = Path("herd-run").resolve()
    command_source = (
        "from pathlib import Path; Path('launcher-cwd.txt').write_text(str(Path.cwd()))"
    )
    environment = dict(
        os.environ,
        TB_STATE_DIR=str(Path("herd-state").resolve()),
        TB_HERD_CMD=f"{shlex.quote(sys.executable)} -I -c {shlex.quote(command_source)}",
    )
    run_cli(
        [
            "run",
            "--arm",
            "herd",
            "--problem",
            "stub",
            "--hours",
            "0.001",
            "--run-id",
            "r-packaging",
            "--workdir",
            "herd-run",
            "--overrides",
            '{"visible_delay_s": 0, "slow_seeds": 1}',
        ],
        environment,
    )
    assert (workdir / "launcher-cwd.txt").read_text() == str(workdir)
    assert (workdir / "fallback-baseline/manifest.yaml").is_file()
    assert not (load_problem("stub").baseline_dir() / "manifest.yaml").exists()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=["cli", "paths", "sandbox", "herd"])
    arguments = parser.parse_args()
    if arguments.check == "cli":
        check_cli()
    elif arguments.check == "paths":
        check_paths()
    elif arguments.check == "sandbox":
        check_sandbox()
    else:
        check_herd()


if __name__ == "__main__":
    main()
else:
    pass
