"""Layer 2 on macOS: a sandbox-exec profile (Seatbelt) around the worker process.

What it guarantees, checked by probe on macOS 15: the process and everything it
starts can open network connections only to the local ports the profile names
(the proxy, and the herd's own local hosts), name resolution included, and cannot
write under the protected directories, which hold the blackboard's bare
repositories. Everything else is left as the parent has it.

sandbox-exec is deprecated by Apple but present on every macOS; its removal is
the day this layer stops being available, and wrap then refuses to start.
"""

import shutil
from pathlib import Path

PROFILE_VERSION = "(version 1)"


def quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_profile(
    local_ports: tuple[int, ...], protected_directories: tuple[Path, ...]
) -> str:
    lines = [
        PROFILE_VERSION,
        "(allow default)",
        "(deny network*)",
        *(
            f'(allow network* (remote ip "localhost:{port}"))'
            for port in sorted(set(local_ports))
        ),
        *(
            f"(deny file-write* (subpath {quoted(str(directory.resolve()))}))"
            for directory in protected_directories
        ),
    ]
    return "\n".join(lines) + "\n"


def sandbox_exec_path() -> str | None:
    return shutil.which("sandbox-exec")


def wrap_argv(profile_path: Path, argv: tuple[str, ...]) -> tuple[str, ...]:
    executable = sandbox_exec_path()
    if executable is None:
        raise RuntimeError("sandbox-exec is not available")
    else:
        return (executable, "-f", str(profile_path), *argv)
