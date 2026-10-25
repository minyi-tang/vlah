"""The vlah command line."""

import argparse
import logging
import sys
from pathlib import Path
from typing import get_args

from vlah import __version__
from vlah.adapters.human.cli.human import CliHuman
from vlah.arms.config import PRESETS
from vlah.cli.charter import init_problem, sign_problem
from vlah.cli.stack import (
    DEFAULT_WORKER_IMAGE,
    check_stack,
    compose_yaml,
    print_checks,
    run_command,
    stack_down,
    stack_up,
)
from vlah.cli.views import (
    show_demo,
    show_events,
    show_inbox,
    show_report,
    show_search,
    show_status,
    stop_run,
)
from vlah.core.types.clock import SystemClock
from vlah.core.types.events import EventType
from vlah.ports.errors import NotSupportedError
from vlah.runtime.command import RunRequest, run_problem

DEFAULT_SEARCH_LIMIT = 20


def add_run_arguments(
    view_parser: argparse.ArgumentParser, has_json: bool
) -> argparse.ArgumentParser:
    """A command about one run of a problem: the latest, or the one --run-id names."""
    view_parser.add_argument("name", type=Path, help="Problem folder.")
    view_parser.add_argument("--run-id", help="Defaults to the run that changed last.")
    if has_json:
        view_parser.add_argument(
            "--json", action="store_true", help="Print JSON instead of text."
        )
    else:
        pass
    return view_parser


def run_stack(arguments: argparse.Namespace) -> int:
    if arguments.stack_command == "up":
        return stack_up(run_command, sys.stdout)
    elif arguments.stack_command == "down":
        return stack_down(run_command, sys.stdout, arguments.run, arguments.volumes)
    elif arguments.stack_command == "check":
        return print_checks(
            check_stack(
                run_command,
                image=arguments.image,
                gateway_url=arguments.gateway_url,
                worker_count=arguments.workers,
                memory_text=arguments.memory,
            ),
            sys.stdout,
        )
    else:
        sys.stdout.write(compose_yaml())
        return 0


def main(argv: list[str] | None = None) -> int:
    """Create, validate, run and watch a problem folder, or run the demo."""
    parser = argparse.ArgumentParser(
        prog="vlah",
        description="Prepare a problem folder for an agent herd, run it and watch it.",
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
    run_identity = run_parser.add_mutually_exclusive_group()
    run_identity.add_argument("--run-id", help="Defaults to a timestamp and the arm.")
    run_identity.add_argument(
        "--resume",
        metavar="RUN_ID",
        help=(
            "Continue a stopped or crashed run with its own arm, workers and seed, "
            "until its original deadline."
        ),
    )
    run_parser.add_argument("--seed", type=int, default=0)
    run_parser.add_argument(
        "--unsafe-no-sandbox",
        action="store_true",
        help="Development only: run workers without OS-level egress enforcement.",
    )
    add_run_arguments(
        commands.add_parser(
            "status", help="Show a run's workers, judge use and baseline."
        ),
        True,
    )
    events_parser = add_run_arguments(
        commands.add_parser("events", help="Print a run's merged events in order."),
        True,
    )
    events_parser.add_argument(
        "--follow", action="store_true", help="Keep printing until the run finishes."
    )
    events_parser.add_argument(
        "--type",
        action="append",
        choices=get_args(EventType),
        default=[],
        help="Only this event type; repeat for more.",
    )
    search_parser = add_run_arguments(
        commands.add_parser(
            "search", help="Find claims and dead ends that hold every word."
        ),
        True,
    )
    search_parser.add_argument("query", help="Words to look for.")
    search_parser.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    add_run_arguments(
        commands.add_parser(
            "report", help="Show the deliverable, the baselines and report/."
        ),
        True,
    )
    add_run_arguments(
        commands.add_parser("inbox", help="List the questions waiting for you."), True
    )
    add_run_arguments(
        commands.add_parser(
            "stop", help="Stop a run cleanly; everything learned is kept."
        ),
        False,
    )
    demo_parser = commands.add_parser(
        "demo", help="Run a whole herd on a toy problem, in memory."
    )
    demo_parser.add_argument("--seed", type=int, default=0)
    stack_parser = commands.add_parser(
        "stack", help="The herd's services on this machine (Docker): up, down, check."
    )
    stack_commands = stack_parser.add_subparsers(dest="stack_command", required=True)
    stack_commands.add_parser(
        "up", help="Start the services (Redis) and wait for them."
    )
    down_parser = stack_commands.add_parser(
        "down", help="Stop the services and remove what runs left behind."
    )
    down_parser.add_argument(
        "--run", metavar="RUN_ID", help="Only this run's containers and networks."
    )
    down_parser.add_argument(
        "--volumes", action="store_true", help="Also remove the Redis volume."
    )
    check_parser = stack_commands.add_parser(
        "check", help="Pre-flight: Docker, the image, Redis, the gateway, memory."
    )
    check_parser.add_argument("--image", default=DEFAULT_WORKER_IMAGE)
    check_parser.add_argument(
        "--gateway-url", help="A model gateway URL as a container would reach it."
    )
    check_parser.add_argument("--workers", type=int, default=1)
    check_parser.add_argument(
        "--memory", default="2g", help="Memory per worker container, such as 2g."
    )
    stack_commands.add_parser("compose", help="Print the compose file the stack uses.")
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
        elif arguments.command == "status":
            show_status(arguments.name, arguments.run_id, arguments.json, sys.stdout)
        elif arguments.command == "events":
            show_events(
                arguments.name,
                arguments.run_id,
                tuple(arguments.type),
                arguments.follow,
                arguments.json,
                sys.stdout,
            )
        elif arguments.command == "search":
            show_search(
                arguments.name,
                arguments.run_id,
                arguments.query,
                arguments.limit,
                arguments.json,
                sys.stdout,
            )
        elif arguments.command == "report":
            show_report(arguments.name, arguments.run_id, arguments.json, sys.stdout)
        elif arguments.command == "inbox":
            show_inbox(
                arguments.name,
                arguments.run_id,
                arguments.json,
                CliHuman(output=sys.stdout, replies=sys.stdin, clock=SystemClock()),
            )
        elif arguments.command == "stop":
            stop_run(arguments.name, arguments.run_id, SystemClock(), sys.stdout)
        elif arguments.command == "demo":
            # The demo's story is its output; the components' progress logs are not.
            logging.basicConfig(level=logging.WARNING, format="%(message)s")
            show_demo(arguments.seed, sys.stdout)
        elif arguments.command == "stack":
            return run_stack(arguments)
        else:
            run_problem(
                RunRequest(
                    problem_root=arguments.name,
                    arm=arguments.arm,
                    worker_count=arguments.workers,
                    duration_hours=arguments.hours,
                    run_id=arguments.resume or arguments.run_id,
                    seed=arguments.seed,
                    allow_unenforced_egress=arguments.unsafe_no_sandbox,
                    is_resume=arguments.resume is not None,
                )
            )
    except (OSError, ValueError, NotSupportedError) as error:
        print(f"vlah: {error}", file=sys.stderr)
        return 2
    return 0
