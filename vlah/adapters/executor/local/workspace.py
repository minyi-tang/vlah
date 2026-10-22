"""One sandbox directory per task: the target worktree, a blackboard clone, the start files.

Layout under <sandbox root>/<agent>/<task id>/:
- the start files (CONTEXT.md and whatever the harness adds), at the top;
- target/, a git worktree of the target system at the task's baseline ref;
- blackboard/, a clone of the blackboard with no remote, refreshed by the sidecar;
- .vlah/, where a harness writes each session's transcript (VLAH_TRANSCRIPT).

The clone has no remote, so the sandbox is never handed the path of the bare
repository, and it is cloned without hardlinks, so its object files are copies.
"""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from vlah.core.types.ids import ActorId
from vlah.ports.executor import AgentSpec

GIT_TIMEOUT_SECONDS = 60.0
TARGET_DIRECTORY = "target"
BLACKBOARD_DIRECTORY = "blackboard"
HERD_DIRECTORY = ".vlah"
# The only variables a worker inherits from the herd's environment. Everything
# else, push credentials included, stays out (D68).
INHERITED_VARIABLES = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
)
FORBIDDEN_VARIABLES = frozenset({"VLAH_PUSH_TOKEN"})


class GitCommandError(RuntimeError):
    pass


def run_git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise GitCommandError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    else:
        return completed.stdout


@dataclass(frozen=True, kw_only=True)
class SandboxPaths:
    root: Path
    target: Path
    blackboard: Path

    def transcript(self, handle_id: str, attempt: int) -> Path:
        """One transcript file per process, so a restart never re-uploads the last one."""
        return self.root / HERD_DIRECTORY / f"{handle_id}-{attempt}.transcript.jsonl"


def sandbox_paths(root: Path) -> SandboxPaths:
    return SandboxPaths(
        root=root,
        target=root / TARGET_DIRECTORY,
        blackboard=root / BLACKBOARD_DIRECTORY,
    )


def local_path(url: str) -> Path | None:
    """The filesystem path behind a file URL or a plain path, or None for a remote URL."""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    elif parsed.scheme == "":
        return Path(url)
    else:
        return None


def refuse_sandbox_inside_run_directory(sandbox_root: Path, clone_url: str) -> None:
    """A sandbox inside the run directory could write the bare repositories' refs (#32)."""
    repository = local_path(clone_url)
    if repository is None:
        return
    else:
        run_directory = repository.resolve().parent
        if sandbox_root.resolve().is_relative_to(run_directory):
            raise ValueError(
                f"sandbox root {sandbox_root} is inside the run directory "
                f"{run_directory}, where the blackboard's bare repositories live"
            )
        else:
            return


def check_out_target(paths: SandboxPaths, spec: AgentSpec) -> str:
    """Create target/ at the task's baseline ref, once; return the commit it started from.

    A sandbox that already exists keeps the agent's work; its base is the ref itself,
    because the agent may have committed on top of it.
    """
    if paths.target.exists():
        return run_git(
            "-C", str(paths.target), "rev-parse", f"{spec.target_ref}^{{commit}}"
        ).strip()
    elif Path(spec.target_repository).is_dir():
        run_git(
            "-C",
            spec.target_repository,
            "worktree",
            "add",
            "-q",
            "--detach",
            str(paths.target),
            spec.target_ref,
        )
    else:
        run_git("clone", "-q", "--no-local", spec.target_repository, str(paths.target))
        run_git("-C", str(paths.target), "checkout", "-q", "--detach", spec.target_ref)
    return run_git("-C", str(paths.target), "rev-parse", "HEAD").strip()


def clone_blackboard(paths: SandboxPaths, clone_url: str) -> None:
    if paths.blackboard.exists():
        return
    else:
        run_git("clone", "-q", "--no-local", clone_url, str(paths.blackboard))
        run_git("-C", str(paths.blackboard), "remote", "remove", "origin")


def refresh_blackboard(paths: SandboxPaths, clone_url: str) -> None:
    """Bring the clone up to the blackboard's head; local edits in it are discarded."""
    run_git("-C", str(paths.blackboard), "fetch", "-q", "--no-tags", clone_url, "HEAD")
    run_git("-C", str(paths.blackboard), "reset", "-q", "--hard", "FETCH_HEAD")


def write_start_files(root: Path, files: Mapping[str, str]) -> None:
    resolved_root = root.resolve()
    for relative_path, text in files.items():
        destination = (root / relative_path).resolve()
        if Path(relative_path).is_absolute() or not destination.is_relative_to(
            resolved_root
        ):
            raise ValueError(f"start file {relative_path!r} is outside the sandbox")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")


def files_touched(paths: SandboxPaths, base_commit: str) -> tuple[str, ...]:
    """Files changed since the baseline, committed or not, plus new untracked files (D32)."""
    changed = run_git(
        "-C", str(paths.target), "diff", "--name-only", base_commit
    ).splitlines()
    untracked = run_git(
        "-C", str(paths.target), "ls-files", "--others", "--exclude-standard"
    ).splitlines()
    return tuple(sorted({*changed, *untracked}))


def worker_environment(
    spec: AgentSpec,
    task_id: str,
    paths: SandboxPaths,
    transcript: Path,
    inherited: Mapping[str, str],
) -> dict[str, str]:
    forbidden = sorted(FORBIDDEN_VARIABLES & spec.env.keys())
    if forbidden:
        raise ValueError(f"a worker environment must not contain {forbidden} (D68)")
    else:
        environment = {
            name: inherited[name] for name in INHERITED_VARIABLES if name in inherited
        }
        environment.update(spec.env)
        environment.update(herd_variables(spec.agent, task_id, paths, transcript))
        return environment


def herd_variables(
    agent: ActorId, task_id: str, paths: SandboxPaths, transcript: Path
) -> dict[str, str]:
    return {
        "VLAH_AGENT": agent,
        "VLAH_TASK": task_id,
        "VLAH_SANDBOX": str(paths.root),
        "VLAH_TARGET": str(paths.target),
        "VLAH_BLACKBOARD": str(paths.blackboard),
        "VLAH_TRANSCRIPT": str(transcript),
    }
