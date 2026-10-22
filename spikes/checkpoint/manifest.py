"""Print a JSON manifest of a sandbox: each path's kind, mode, size and SHA-256, plus the git state.

Run it as the worker user, because git refuses repositories owned by someone else.
"""

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

READ_CHUNK_BYTES = 1 << 20


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_entries(root: Path) -> dict[str, list[str | int]]:
    """Every path under root except .git, keyed by its POSIX relative path."""
    entries: dict[str, list[str | int]] = {}
    for directory, subdirectory_names, file_names in os.walk(root):
        subdirectory_names[:] = sorted(
            name
            for name in subdirectory_names
            if not (directory == str(root) and name == ".git")
        )
        for name in subdirectory_names + sorted(file_names):
            path = Path(directory) / name
            relative_path = path.relative_to(root).as_posix()
            status = path.lstat()
            mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISLNK(status.st_mode):
                entries[relative_path] = ["symlink", mode, 0, os.readlink(path)]
            elif stat.S_ISDIR(status.st_mode):
                entries[relative_path] = ["directory", mode, 0, ""]
            elif stat.S_ISREG(status.st_mode):
                entries[relative_path] = [
                    "file",
                    mode,
                    status.st_size,
                    file_digest(path),
                ]
            else:
                entries[relative_path] = ["other", mode, 0, ""]
    return entries


def git_state(root: Path) -> dict[str, str]:
    commands = {
        "head_commit": ["rev-parse", "HEAD"],
        "branch": ["symbolic-ref", "--short", "HEAD"],
        "status": ["status", "--porcelain=v1"],
        "staged_diff": ["diff", "--cached"],
        "unstaged_diff": ["diff"],
        "local_config": ["config", "--local", "--list"],
    }
    return {
        name: subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for name, arguments in commands.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parsed = parser.parse_args()
    print(
        json.dumps({"tree": tree_entries(parsed.root), "git": git_state(parsed.root)})
    )


if __name__ == "__main__":
    main()
