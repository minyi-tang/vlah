"""Agent processes: launched in their own process group, reaped once, signalled as a group.

The executor reaps each child itself with os.wait4, to get its resource use, and then
tells Popen the exit code, so Popen never reaps again and reads a lost child as 0.
"""

import logging
import math
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from vlah.adapters.executor.local.traces import SessionLogs
from vlah.adapters.executor.sandbox.api import WrappedCommand
from vlah.core.types.clock import Clock
from vlah.ports.executor import ExitInfo

logger = logging.getLogger(__name__)

REAP_POLL_SECONDS = 0.05


@dataclass(frozen=True, kw_only=True)
class ResourceUsage:
    """What one process used, for diagnostics; CPU and memory cover the direct child only."""

    attempt: int
    wall_seconds: float
    cpu_seconds: float
    max_rss_bytes: int


@dataclass(kw_only=True)
class AgentProcess:
    popen: subprocess.Popen[bytes]
    attempt: int
    logs: SessionLogs
    started_seconds: float
    exit_info: ExitInfo | None = None
    usage: ResourceUsage | None = None


def max_rss_bytes(max_rss: int) -> int:
    """ru_maxrss is bytes on macOS and KiB on Linux."""
    if sys.platform == "darwin":
        return max_rss
    else:
        return max_rss * 1024


def launch_process(
    command: WrappedCommand,
    working_directory: Path,
    logs: SessionLogs,
    attempt: int,
    clock: Clock,
) -> AgentProcess:
    with logs.stdout.open("wb") as stdout, logs.stderr.open("wb") as stderr:
        popen = subprocess.Popen(
            list(command.argv),
            cwd=working_directory,
            env=command.env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    return AgentProcess(
        popen=popen, attempt=attempt, logs=logs, started_seconds=clock.now_seconds()
    )


def signal_group(process: AgentProcess, signal_number: int) -> None:
    """Signal the whole process group, so tools the agent started stop with it."""
    try:
        os.killpg(process.popen.pid, signal_number)
    except (ProcessLookupError, PermissionError):
        pass


def reap(process: AgentProcess, blocking: bool, clock: Clock) -> ExitInfo | None:
    """Collect the exit status and resource use, once; then stop anything left in the group."""
    if process.exit_info is not None:
        return process.exit_info
    else:
        try:
            pid, status, rusage = os.wait4(
                process.popen.pid, 0 if blocking else os.WNOHANG
            )
        except ChildProcessError:
            logger.warning(f"process {process.popen.pid} was reaped elsewhere")
            process.exit_info = ExitInfo(return_code=None, was_killed=True)
            return process.exit_info
        if pid == 0:
            return None
        else:
            return_code = os.waitstatus_to_exitcode(status)
            process.popen.returncode = return_code
            signal_group(process, signal.SIGKILL)
            process.usage = ResourceUsage(
                attempt=process.attempt,
                wall_seconds=clock.now_seconds() - process.started_seconds,
                cpu_seconds=rusage.ru_utime + rusage.ru_stime,
                max_rss_bytes=max_rss_bytes(rusage.ru_maxrss),
            )
            process.exit_info = ExitInfo(
                return_code=None if return_code < 0 else return_code,
                was_killed=return_code < 0,
            )
            return process.exit_info


def reap_within(process: AgentProcess, seconds: float, clock: Clock) -> ExitInfo | None:
    """Poll for the exit for about this many real seconds."""
    for _ in range(max(1, math.ceil(seconds / REAP_POLL_SECONDS))):
        exit_info = reap(process, blocking=False, clock=clock)
        if exit_info is not None:
            return exit_info
        else:
            time.sleep(REAP_POLL_SECONDS)
    return reap(process, blocking=False, clock=clock)


def stop(process: AgentProcess, grace_seconds: float, clock: Clock) -> ExitInfo:
    """SIGTERM the group, wait out the grace period, then SIGKILL (D60 layer 3)."""
    signal_group(process, signal.SIGTERM)
    exit_info = reap_within(process, grace_seconds, clock)
    if exit_info is not None:
        return exit_info
    else:
        signal_group(process, signal.SIGKILL)
        killed = reap(process, blocking=True, clock=clock)
        assert killed is not None
        return killed


def kill(process: AgentProcess, clock: Clock) -> ExitInfo:
    signal_group(process, signal.SIGKILL)
    killed = reap(process, blocking=True, clock=clock)
    assert killed is not None
    return killed
