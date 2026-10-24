"""The vlah command line."""

import argparse
import sys
from pathlib import Path

from vlah import __version__
from vlah.arms.config import PRESETS
from vlah.cli.charter import init_problem, sign_problem
from vlah.ports.errors import NotSupportedError
from vlah.runtime.command import RunRequest, run_problem


def main(argv: list[str] | None = None) -> int:
    """Create, validate and run a problem folder; the other commands are tracked in #23."""
    parser = argparse.ArgumentParser(
        prog="vlah",
        description="Prepare a problem folder for an agent herd and run it.",
    )
    parser.add_argument("--version", action="version", version=f"vlah {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init", help="Create a draft problem folder.")
    init_parser.add_argument("name", type=Path, help="Problem folder to create.")
    init_parser.add_argument(
        "--repo", type=Path, required=True, help="Existing target git working tree."
    )
    sign_parser = commands.add_parser("sign", help="Validate and sign a charter.")
    sign_parser.add_argument("name", type=Path, help="Problem folder to validate.")
    run_parser = commands.add_parser(
        "run", help="Run one arm of a herd on this machine."
    )
    run_parser.add_argument("name", type=Path, help="Signed problem folder.")
    run_parser.add_argument("--arm", choices=sorted(PRESETS), default="S2")
    run_parser.add_argument("--workers", type=int, default=2, help="Worker count.")
    run_parser.add_argument("--hours", type=float, default=1.0, help="Run length.")
    run_parser.add_argument("--run-id", help="Defaults to a timestamp and the arm.")
    run_parser.add_argument("--seed", type=int, default=0)
    run_parser.add_argument(
        "--unsafe-no-sandbox",
        action="store_true",
        help="Development only: run workers without OS-level egress enforcement.",
    )
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exit_status:
        return exit_status.code if isinstance(exit_status.code, int) else 2

    try:
        if arguments.command == "init":
            root = init_problem(arguments.name, arguments.repo)
            print(
                f"Created {root}. Set charter/constraints.md limits, declare sources "
                "and write the charter before running vlah sign."
            )
        elif arguments.command == "sign":
            status_path = sign_problem(arguments.name)
            print(f"Aligned {status_path.parent.parent}. No run was started.")
        else:
            run_problem(
                RunRequest(
                    problem_root=arguments.name,
                    arm=arguments.arm,
                    worker_count=arguments.workers,
                    duration_hours=arguments.hours,
                    run_id=arguments.run_id,
                    seed=arguments.seed,
                    allow_unenforced_egress=arguments.unsafe_no_sandbox,
                )
            )
    except (OSError, ValueError, NotSupportedError) as error:
        print(f"vlah: {error}", file=sys.stderr)
        return 2
    return 0
