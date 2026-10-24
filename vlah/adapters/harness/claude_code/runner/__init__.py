"""Runs Claude Code with a private config directory and copies its stream-json output.

- CLAUDE_CONFIG_DIR is the sandbox's .vlah/claude/home, so a worker never loads the
  host user's settings, MCP servers, plugins, memory or sessions. A worker signs in
  with what the harness passes in its environment: an API key, or a gateway's base
  URL and token (for example a LiteLLM proxy), never the user's own login.
- stdout is copied to VLAH_TRANSCRIPT line by line as it arrives, for the worker
  loop and the hook, and still passes through to the executor's log. The exit code
  is Claude Code's.
"""

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

HOME_DIRECTORY = ".vlah/claude/home"


def copy_lines(source: IO[bytes], destinations: Sequence[IO[bytes]]) -> None:
    for line in source:
        for destination in destinations:
            destination.write(line)
            destination.flush()


def main(
    command: Sequence[str],
    environment: Mapping[str, str],
    working_directory: Path,
    stdout: IO[bytes],
) -> int:
    if not command:
        print("usage: python -m ...runner <claude command...>", file=sys.stderr)
        return 2
    else:
        transcript = Path(environment["VLAH_TRANSCRIPT"])
        transcript.parent.mkdir(parents=True, exist_ok=True)
        home = (working_directory / HOME_DIRECTORY).resolve()
        home.mkdir(parents=True, exist_ok=True)
        with (
            transcript.open("ab") as copy,
            subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                env={**environment, "CLAUDE_CONFIG_DIR": str(home)},
            ) as process,
        ):
            assert process.stdout is not None
            copy_lines(process.stdout, (stdout, copy))
        return process.returncode
