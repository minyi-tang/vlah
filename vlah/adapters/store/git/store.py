"""A VersionedStore on a bare git repository (v2.2 D30, D56, D68).

Each merge is one git fast-import run that writes the files, the sequence pointers,
the tree and one commit on main. The commit's trailers record the sequence numbers
it assigned, so the sequence index can be rebuilt from the repository alone. A
pre-receive hook rejects any push without the herd's push token, so a worker's
clone is effectively read-only.
"""

import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vlah.core.types.ids import ActorId
from vlah.ports.errors import AppendOnlyViolationError, PushRejectedError
from vlah.ports.store import MergeListener, MergeResult

MAIN_REF = "refs/heads/main"
EVENT_FILE_PATTERN = re.compile(r"events/(?!_seq/)[^/]+/[^/]+\.json")
SEQUENCE_POINTER_PREFIX = "events/_seq/"
SEQUENCE_TRAILER = "Herd-Seq"
AUTHOR_TRAILER = "Herd-Author"
COMMIT_ID_TRAILER = "Herd-Commit"
MERGE_QUEUE_NAME = "vlah merge queue"
MERGE_QUEUE_EMAIL = "merge-queue@vlah.invalid"
PRE_RECEIVE_HOOK = """#!/bin/sh
# Only the herd's tool server may push (v2.2 D68).
token_hash=$(printf %s "${VLAH_PUSH_TOKEN}" | git hash-object --stdin)
if [ "$token_hash" != "EXPECTED_TOKEN_HASH" ]; then
    echo "pushes to the blackboard need the herd push token" >&2
    exit 1
else
    exit 0
fi
"""


@dataclass(frozen=True, kw_only=True)
class PendingGitCommit:
    commit_id: str
    author: ActorId
    message: str
    files: dict[str, bytes]


def fast_import_path(path: str) -> str:
    """A path as git fast-import expects it: quoted only when it has to be."""
    if "\n" in path:
        raise ValueError(f"paths cannot contain newlines: {path!r}")
    elif path.startswith('"') or " " in path or "\\" in path:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    else:
        return path


def fast_import_data(content: bytes) -> bytes:
    return f"data {len(content)}\n".encode() + content + b"\n"


class GitVersionedStore:
    """Append-only merges into main, one sequence number per new event file."""

    def __init__(self, *, repository_path: Path, push_token: str) -> None:
        self.repository_path = repository_path
        self.push_token = push_token
        self.lock = threading.Lock()
        self.pending_by_branch: dict[str, list[PendingGitCommit]] = {}
        self.listeners: dict[int, MergeListener] = {}
        self.next_subscription_id = 1
        if not (repository_path / "HEAD").exists():
            repository_path.mkdir(parents=True, exist_ok=True)
            self.git("init", "--bare", "--quiet", "--initial-branch", "main")
        else:
            pass
        self.install_pre_receive_hook()
        self.commit_by_sequence: dict[int, str] = {}
        self.main_paths: set[str] = set()
        self.main_head: str | None = None
        self.commit_count = 0
        self.rebuild_index()

    def environment(self) -> dict[str, str]:
        return {**os.environ, "GIT_DIR": str(self.repository_path)}

    def git(self, *arguments: str, input_bytes: bytes | None = None) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            input=input_bytes,
            capture_output=True,
            env=self.environment(),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(arguments)} failed: {completed.stderr.decode(errors='replace')}"
            )
        else:
            return completed.stdout

    def install_pre_receive_hook(self) -> None:
        token_hash = self.git(
            "hash-object", "--stdin", input_bytes=self.push_token.encode()
        )
        hook_path = self.repository_path / "hooks" / "pre-receive"
        hook_path.parent.mkdir(exist_ok=True)
        hook_path.write_text(
            PRE_RECEIVE_HOOK.replace(
                "EXPECTED_TOKEN_HASH", token_hash.decode().strip()
            ),
            encoding="utf-8",
        )
        hook_path.chmod(0o755)

    def rebuild_index(self) -> None:
        """Rebuild the sequence index, main's paths and the commit count from the repository."""
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", MAIN_REF],
            capture_output=True,
            env=self.environment(),
            check=False,
        )
        if completed.returncode != 0:
            self.main_head = None
            self.main_paths = set()
            self.commit_by_sequence = {}
            self.commit_count = 0
        else:
            self.main_head = completed.stdout.decode().strip()
            log_output = self.git(
                "log", "--format=%H%x00%B%x01", self.main_head
            ).decode()
            self.commit_by_sequence = {}
            self.commit_count = 0
            for entry in log_output.split("\x01"):
                if "\x00" in entry:
                    commit_hash, body = entry.strip("\n").split("\x00", 1)
                    for sequence_number in parse_sequence_trailer(body):
                        self.commit_by_sequence[sequence_number] = commit_hash
                    # Rejected commits also used ids, so continue from the largest one seen.
                    self.commit_count = max(
                        self.commit_count, parse_commit_number(body)
                    )
                else:
                    continue
            self.main_paths = set(self.tree_paths(self.main_head))

    def tree_paths(self, revision: str) -> list[str]:
        output = self.git("ls-tree", "-r", "--name-only", "-z", revision)
        return [path for path in output.decode().split("\x00") if path]

    def commit(
        self,
        branch: str,
        files: Mapping[str, bytes],
        author: ActorId,
        message: str,
        push_token: str,
    ) -> str:
        if push_token != self.push_token:
            raise PushRejectedError(f"commit by {author} has no push token")
        elif not files:
            raise ValueError("a commit needs at least one file")
        else:
            with self.lock:
                self.commit_count += 1
                commit_id = f"{self.repository_path.stem}-{self.commit_count:06d}"
                self.pending_by_branch.setdefault(branch, []).append(
                    PendingGitCommit(
                        commit_id=commit_id,
                        author=author,
                        message=message,
                        files=dict(files),
                    )
                )
                return commit_id

    def merge(self, branch: str) -> tuple[MergeResult, ...]:
        merged: list[MergeResult] = []
        with self.lock:
            pending_commits = self.pending_by_branch.pop(branch, [])
        for index, pending_commit in enumerate(pending_commits):
            with self.lock:
                existing_paths = sorted(set(pending_commit.files) & self.main_paths)
                if existing_paths:
                    remaining_commits = pending_commits[index + 1 :]
                    if remaining_commits:
                        self.pending_by_branch[branch] = remaining_commits
                    else:
                        pass
                    raise AppendOnlyViolationError(
                        f"{pending_commit.commit_id} would overwrite {existing_paths}"
                    )
                else:
                    merge_result = self.merge_one(pending_commit)
            merged.append(merge_result)
            for listener in list(self.listeners.values()):
                listener(merge_result)
        return tuple(merged)

    def merge_one(self, pending_commit: PendingGitCommit) -> MergeResult:
        """Put one pending commit on main with a single fast-import run; the caller holds the lock."""
        head_sequence = max(self.commit_by_sequence, default=0)
        event_sequence_numbers: dict[str, int] = {}
        files = dict(pending_commit.files)
        for path in sorted(pending_commit.files):
            if EVENT_FILE_PATTERN.fullmatch(path):
                head_sequence += 1
                event_sequence_numbers[path] = head_sequence
                files[f"{SEQUENCE_POINTER_PREFIX}{head_sequence}"] = path.encode(
                    "utf-8"
                )
            else:
                pass
        trailers = [
            f"{AUTHOR_TRAILER}: {pending_commit.author}",
            f"{COMMIT_ID_TRAILER}: {pending_commit.commit_id}",
        ]
        if event_sequence_numbers:
            sequence_values = sorted(event_sequence_numbers.values())
            trailers.append(
                f"{SEQUENCE_TRAILER}: {sequence_values[0]}-{sequence_values[-1]}"
            )
        else:
            pass
        message = f"{pending_commit.message}\n\n" + "\n".join(trailers) + "\n"
        new_head = self.fast_import(files, message)
        for sequence_number in event_sequence_numbers.values():
            self.commit_by_sequence[sequence_number] = new_head
        self.main_head = new_head
        self.main_paths.update(files)
        pointer_paths = tuple(
            f"{SEQUENCE_POINTER_PREFIX}{sequence_number}"
            for sequence_number in sorted(event_sequence_numbers.values())
        )
        return MergeResult(
            commit_id=pending_commit.commit_id,
            author=pending_commit.author,
            added_paths=tuple(sorted(pending_commit.files)) + pointer_paths,
            event_sequence_numbers=event_sequence_numbers,
        )

    def fast_import(self, files: dict[str, bytes], message: str) -> str:
        """Write files on top of main as one commit; returns the new commit hash."""
        stream = [
            f"commit {MAIN_REF}\nmark :1\n".encode(),
            f"committer {MERGE_QUEUE_NAME} <{MERGE_QUEUE_EMAIL}> now\n".encode(),
            fast_import_data(message.encode("utf-8")),
        ]
        if self.main_head is not None:
            stream.append(f"from {self.main_head}\n".encode())
        else:
            pass
        for path, content in sorted(files.items()):
            stream.append(f"M 100644 inline {fast_import_path(path)}\n".encode("utf-8"))
            stream.append(fast_import_data(content))
        stream.append(b"done\n")
        with tempfile.TemporaryDirectory() as scratch_directory:
            marks_path = Path(scratch_directory) / "marks"
            self.git(
                "fast-import",
                "--quiet",
                "--done",
                "--date-format=now",
                f"--export-marks={marks_path}",
                input_bytes=b"".join(stream),
            )
            return marks_path.read_text(encoding="utf-8").split()[1]

    def revision_for(self, at_sequence: int | None) -> str | None:
        if at_sequence is None:
            return self.main_head
        elif at_sequence in self.commit_by_sequence:
            return self.commit_by_sequence[at_sequence]
        else:
            raise ValueError(f"no merge assigned sequence number {at_sequence}")

    def read(self, path: str, at_sequence: int | None) -> bytes:
        revision = self.revision_for(at_sequence)
        if revision is None:
            raise FileNotFoundError(path)
        else:
            completed = subprocess.run(
                ["git", "cat-file", "blob", f"{revision}:{path}"],
                capture_output=True,
                env=self.environment(),
                check=False,
            )
            if completed.returncode == 0:
                return completed.stdout
            else:
                raise FileNotFoundError(path)

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]:
        if at_sequence is None:
            return tuple(
                sorted(path for path in self.main_paths if path.startswith(prefix))
            )
        else:
            revision = self.revision_for(at_sequence)
            assert revision is not None
            return tuple(
                sorted(
                    path
                    for path in self.tree_paths(revision)
                    if path.startswith(prefix)
                )
            )

    def exists(self, path: str) -> bool:
        return path in self.main_paths

    def head_sequence(self) -> int:
        return max(self.commit_by_sequence, default=0)

    def subscribe(self, listener: MergeListener) -> int:
        subscription_id = self.next_subscription_id
        self.next_subscription_id += 1
        self.listeners[subscription_id] = listener
        return subscription_id

    def unsubscribe(self, subscription_id: int) -> None:
        self.listeners.pop(subscription_id)

    def clone_url(self, read_only: bool) -> str:
        """A file URL; pushes to it are rejected by the pre-receive hook either way."""
        return self.repository_path.resolve().as_uri()


def parse_sequence_trailer(commit_body: str) -> list[int]:
    for line in commit_body.splitlines():
        if line.startswith(f"{SEQUENCE_TRAILER}: "):
            first_text, last_text = line.removeprefix(f"{SEQUENCE_TRAILER}: ").split(
                "-"
            )
            return list(range(int(first_text), int(last_text) + 1))
        else:
            continue
    return []


def parse_commit_number(commit_body: str) -> int:
    for line in commit_body.splitlines():
        if line.startswith(f"{COMMIT_ID_TRAILER}: "):
            return int(line.rsplit("-", 1)[1])
        else:
            continue
    return 0
