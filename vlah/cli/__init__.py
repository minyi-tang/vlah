"""The vlah command line."""

import argparse
import sys
from pathlib import Path

from vlah import __version__
from vlah.cli.charter import init_problem, sign_problem


def main(argv: list[str] | None = None) -> int:
    """Create and validate a problem charter; run commands are tracked in #23."""
    parser = argparse.ArgumentParser(
        prog="vlah", description="Prepare a problem folder for an agent herd."
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
        else:
            status_path = sign_problem(arguments.name)
            print(f"Aligned {status_path.parent.parent}. No run was started.")
    except (OSError, ValueError) as error:
        print(f"vlah: {error}", file=sys.stderr)
        return 2
    return 0
