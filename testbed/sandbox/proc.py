"""Parent-side wrapper for running untrusted candidate code in a child process.

Framing: 4-byte big-endian length + payload. Parent -> child is pickle (the child trusts
the parent); child -> parent is JSON so a malicious child cannot run code in the parent.

Isolation is best effort (anti-cheat is not this testbed's goal, see spec 0/2):
  * cwd is a fresh temp dir holding only the copied candidate and host script;
  * the host blocks socket before importing the candidate;
  * on macOS, sandbox-exec denies network and reads of hidden testbed paths
    (set TB_SANDBOX=off to disable); on Linux, unshare -n is used when permitted.
"""

from __future__ import annotations

import json
import os
import pickle
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from testbed.problem_api import PROBLEMS_ROOT, STATE_DIR, private_dirs


class HostDied(RuntimeError):
    pass


class HostTimeout(RuntimeError):
    pass


def hidden_paths() -> list[Path]:
    paths = [STATE_DIR] + private_dirs()
    for problem_dir in PROBLEMS_ROOT.iterdir() if PROBLEMS_ROOT.exists() else []:
        for name in (
            "hidden",
            "reference",
            "probes",
            "planted",
            "problem.yaml",
            "tasks/holdout",
        ):
            paths.append(problem_dir / name)
    return paths


def sandbox_prefix() -> list[str]:
    mode = os.environ.get("TB_SANDBOX", "auto")
    if mode in (
        "off",
        "inherit",
    ):  # inherit: already inside the agent's sandbox (no nesting on macOS)
        return []
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        rules = ["(version 1)", "(allow default)", "(deny network*)"]
        for hidden_path in hidden_paths():
            rules.append(f'(deny file-read* (subpath "{hidden_path}"))')
        return ["sandbox-exec", "-p", " ".join(rules)]
    if (
        sys.platform.startswith("linux")
        and shutil.which("unshare")
        and mode == "strict"
    ):
        return ["unshare", "-rn"]
    return []


class HostProcess:
    """One long-lived child. request sends one message and waits for one reply."""

    def __init__(
        self,
        host_files: list[Path],
        entry: str,
        extra_files: dict[str, Path] | None = None,
    ):
        self.workdir = Path(tempfile.mkdtemp(prefix="tb-sbx-"))
        for host_file in host_files:
            shutil.copy2(host_file, self.workdir / host_file.name)
        for name, source in (extra_files or {}).items():
            destination = self.workdir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.workdir),
        }
        argv = sandbox_prefix() + [sys.executable, "-I", str(self.workdir / entry)]
        self.child = subprocess.Popen(
            argv,
            cwd=self.workdir,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.is_dead = False

    def read_exact(self, byte_count: int, deadline_s: float) -> bytes:
        received = b""
        stdout_descriptor = self.child.stdout.fileno()
        deadline_at = time.monotonic() + deadline_s
        while len(received) < byte_count:
            remaining_seconds = deadline_at - time.monotonic()
            if remaining_seconds <= 0:
                raise HostTimeout("child did not answer in time")
            readable, _, _ = select.select(
                [stdout_descriptor], [], [], remaining_seconds
            )
            if not readable:
                continue
            chunk = os.read(stdout_descriptor, byte_count - len(received))
            if not chunk:
                raise HostDied("child exited")
            received += chunk
        return received

    def request(self, message, deadline_s: float = 30.0) -> dict:
        if self.is_dead:
            raise HostDied("child already dead")
        message_bytes = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
        try:
            self.child.stdin.write(
                struct.pack(">I", len(message_bytes)) + message_bytes
            )
            self.child.stdin.flush()
            (reply_length,) = struct.unpack(">I", self.read_exact(4, deadline_s))
            reply_bytes = self.read_exact(reply_length, deadline_s)
        except (BrokenPipeError, HostDied, HostTimeout, OSError) as error:
            self.kill()
            if isinstance(error, HostTimeout):
                raise
            raise HostDied(str(error)) from error
        return json.loads(reply_bytes.decode())

    def kill(self):
        self.is_dead = True
        try:
            self.child.kill()
            self.child.wait(timeout=5)
        except Exception:
            pass

    def close(self):
        if not self.is_dead:
            try:
                self.request({"op": "exit"}, deadline_s=2)
            except Exception:
                pass
            self.kill()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
