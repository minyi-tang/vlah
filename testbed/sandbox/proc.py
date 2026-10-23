"""Parent-side wrapper for running untrusted candidate code in a child process.

Framing: 4-byte big-endian length + payload. Parent -> child is pickle (the child trusts
the parent); child -> parent is JSON so a malicious child cannot run code in the parent.

Isolation is best effort (anti-cheat is not this testbed's goal, see spec 0/2):
  * cwd is a fresh temp dir holding only the copied candidate and host script;
  * the host blocks ``socket`` before importing the candidate;
  * on macOS, ``sandbox-exec`` denies network and reads of hidden testbed paths
    (set TB_SANDBOX=off to disable); on Linux, ``unshare -n`` is used when permitted.
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
from pathlib import Path

from testbed.problem_api import PROBLEMS_ROOT, STATE_DIR, private_dirs


class HostDied(RuntimeError):
    pass


class HostTimeout(RuntimeError):
    pass


def _hidden_paths() -> list[Path]:
    out = [STATE_DIR] + private_dirs()
    for prob in PROBLEMS_ROOT.iterdir() if PROBLEMS_ROOT.exists() else []:
        for name in (
            "hidden",
            "reference",
            "probes",
            "planted",
            "problem.yaml",
            "tasks/holdout",
        ):
            out.append(prob / name)
    return out


def _sandbox_prefix() -> list[str]:
    mode = os.environ.get("TB_SANDBOX", "auto")
    if mode in (
        "off",
        "inherit",
    ):  # inherit: already inside the agent's sandbox (no nesting on macOS)
        return []
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        rules = ["(version 1)", "(allow default)", "(deny network*)"]
        for p in _hidden_paths():
            rules.append(f'(deny file-read* (subpath "{p}"))')
        return ["sandbox-exec", "-p", " ".join(rules)]
    if (
        sys.platform.startswith("linux")
        and shutil.which("unshare")
        and mode == "strict"
    ):
        return ["unshare", "-rn"]
    return []


class HostProcess:
    """One long-lived child. ``request`` sends one message and waits for one reply."""

    def __init__(
        self,
        host_files: list[Path],
        entry: str,
        extra_files: dict[str, Path] | None = None,
    ):
        self.tmp = Path(tempfile.mkdtemp(prefix="tb-sbx-"))
        for f in host_files:
            shutil.copy2(f, self.tmp / f.name)
        for name, src in (extra_files or {}).items():
            dst = self.tmp / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(self.tmp),
        }
        argv = _sandbox_prefix() + [sys.executable, "-I", str(self.tmp / entry)]
        self.proc = subprocess.Popen(
            argv,
            cwd=self.tmp,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.dead = False

    def _read_exact(self, n: int, deadline_s: float) -> bytes:
        buf = b""
        fd = self.proc.stdout.fileno()
        import time

        end = time.monotonic() + deadline_s
        while len(buf) < n:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise HostTimeout("child did not answer in time")
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                continue
            chunk = os.read(fd, n - len(buf))
            if not chunk:
                raise HostDied("child exited")
            buf += chunk
        return buf

    def request(self, obj, deadline_s: float = 30.0) -> dict:
        if self.dead:
            raise HostDied("child already dead")
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        try:
            self.proc.stdin.write(struct.pack(">I", len(data)) + data)
            self.proc.stdin.flush()
            (n,) = struct.unpack(">I", self._read_exact(4, deadline_s))
            payload = self._read_exact(n, deadline_s)
        except (BrokenPipeError, HostDied, HostTimeout, OSError) as e:
            self.kill()
            if isinstance(e, HostTimeout):
                raise
            raise HostDied(str(e)) from e
        return json.loads(payload.decode())

    def kill(self):
        self.dead = True
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def close(self):
        if not self.dead:
            try:
                self.request({"op": "exit"}, deadline_s=2)
            except Exception:
                pass
            self.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
