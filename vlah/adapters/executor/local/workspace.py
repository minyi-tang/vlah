"""One sandbox directory per task: the target worktree, a blackboard clone, the start files.

Layout under <sandbox root>/<agent>/<task id>/:
- the start files (CONTEXT.md and whatever the harness adds), at the top;
- target/, a clone of the target system at the task's baseline ref, with no remote;
- blackboard/, a clone of the blackboard with no remote, refreshed by the sidecar;
- .vlah/, where a harness writes each session's transcript (VLAH_TRANSCRIPT).

Both clones are made without hardlinks, so their object files are copies, and
have no remote, so the sandbox is never handed the path of the herd's repositories:
a git worktree would have shared the target's .git, letting a worker write its refs
and objects. Each refresh deletes FETCH_HEAD, which would name the bare repository.
The commit the target started from is kept as refs/vlah/base inside the clone.
"""

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from vlah.core.types.ids import ActorId
from vlah.ports.executor import AgentSpec

GIT_TIMEOUT_SECONDS = 60.0
TARGET_DIRECTORY = "target"
BASE_REF = "refs/vlah/base"
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
GIT_SETTINGS_PREFIX = "GIT_CONFIG_"


class GitCommandError(RuntimeError):
    pass


def run_git(*arguments: str, environment: Mapping[str, str] | None = None) -> str:
    """environment is added to the herd's own: the git settings a view needs (#129)."""
    completed = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env={**os.environ, **(environment or {})},
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

    A sandbox that already exists keeps the agent's work, and its base is the commit
    recorded when it was created, since the agent may have committed on top of it.
    """
    target = str(paths.target)
    if paths.target.exists():
        return run_git("-C", target, "rev-parse", BASE_REF).strip()
    else:
        run_git("clone", "-q", "--no-local", spec.target_repository, target)
        base_commit = resolve_in_clone(target, spec.target_ref)
        run_git("-C", target, "checkout", "-q", "--detach", base_commit)
        run_git("-C", target, "update-ref", BASE_REF, base_commit)
        forget_origin(paths.target)
        return base_commit


def forget_origin(clone: Path) -> None:
    """Remove every place a clone records where it came from: remote, reflogs, FETCH_HEAD."""
    remotes = run_git("-C", str(clone), "remote").split()
    if "origin" in remotes:
        run_git("-C", str(clone), "remote", "remove", "origin")
    else:
        pass
    shutil.rmtree(clone / ".git" / "logs", ignore_errors=True)
    (clone / ".git" / "FETCH_HEAD").unlink(missing_ok=True)


def resolve_in_clone(clone: str, ref: str) -> str:
    """A ref as the source named it: a local branch, a tag, a commit, or origin's branch."""
    for candidate in (ref, f"origin/{ref}"):
        try:
            return run_git(
                "-C", clone, "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}"
            ).strip()
        except GitCommandError:
            continue
    run_git("-C", clone, "fetch", "-q", "origin", ref)
    return run_git("-C", clone, "rev-parse", "FETCH_HEAD^{commit}").strip()


class ViewSettings(Protocol):
    def __call__(self, agent: ActorId) -> Mapping[str, str]:
        """Git settings that open this agent's view of the blackboard (#129)."""
        ...


def view_environment(
    spec: AgentSpec, view_settings: ViewSettings | None
) -> dict[str, str]:
    """What the clone and refresh run with: the session's GIT_CONFIG_* entries and,
    when the blackboard is served per worker, that worker's own settings."""
    return {
        **git_settings(spec.env),
        **(dict(view_settings(spec.agent)) if view_settings is not None else {}),
    }


def git_settings(environment: Mapping[str, str]) -> dict[str, str]:
    """The GIT_CONFIG_* entries of a session's environment: how a view over HTTP is
    opened (its token as an extra header), and nothing else of the session's."""
    return {
        name: value
        for name, value in environment.items()
        if name.startswith(GIT_SETTINGS_PREFIX)
    }


def clone_blackboard(
    paths: SandboxPaths, clone_url: str, environment: Mapping[str, str] | None = None
) -> None:
    if paths.blackboard.exists():
        return
    else:
        run_git(
            "clone",
            "-q",
            "--no-local",
            clone_url,
            str(paths.blackboard),
            environment=environment,
        )
        forget_origin(paths.blackboard)


def refresh_blackboard(
    paths: SandboxPaths, clone_url: str, environment: Mapping[str, str] | None = None
) -> None:
    """Bring the clone up to the blackboard's head; local edits in it are discarded."""
    run_git(
        "-C",
        str(paths.blackboard),
        "fetch",
        "-q",
        "--no-tags",
        clone_url,
        "HEAD",
        environment=environment,
    )
    run_git("-C", str(paths.blackboard), "reset", "-q", "--hard", "FETCH_HEAD")
    forget_origin(paths.blackboard)


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
