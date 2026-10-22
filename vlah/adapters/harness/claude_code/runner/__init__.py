"""Runs Claude Code and copies its stream-json output to VLAH_TRANSCRIPT as it arrives.

The worker loop and the hook read context use from that copy while the session runs,
and the executor archives it as the session's transcript. Everything still goes to
stdout too, which the executor keeps as the session's log. The exit code is Claude
Code's.
"""

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO


def copy_lines(source: IO[bytes], destinations: Sequence[IO[bytes]]) -> None:
    for line in source:
        for destination in destinations:
            destination.write(line)
            destination.flush()


def main(
    command: Sequence[str], environment: Mapping[str, str], stdout: IO[bytes]
) -> int:
    if not command:
        print("usage: python -m ...runner <claude command...>", file=sys.stderr)
        return 2
    else:
        transcript = Path(environment["VLAH_TRANSCRIPT"])
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with (
            transcript.open("ab") as copy,
            subprocess.Popen(
                list(command), stdout=subprocess.PIPE, env=dict(environment)
            ) as process,
        ):
            assert process.stdout is not None
            copy_lines(process.stdout, (stdout, copy))
        return process.returncode
