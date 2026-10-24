"""Reading a run from outside its process: its directory and its blackboard (#119).

A run lives in <problem>/.herd/runs/<run id>/: status.json from the supervisor,
report.json once vlah run has ended, and the blackboard's bare git repository.
The blackboard is read with git plumbing on refs/heads/main only. Opening its
store would reinstall the push hook with a new token, a write into a live run.
"""

import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from vlah.core.types.base import FrozenModel
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.claims import parse_claim
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.states import ClaimType
from vlah.runtime.command import RUNS_DIRECTORY
from vlah.runtime.local_assembly import BLACKBOARD_NAME
from vlah.runtime.status import STATUS_FILE_NAME, RunStatusFile, read_status

MAIN_REF = "refs/heads/main"
SEQUENCE_POINTER_PREFIX = "events/_seq/"
REPORT_FILE_NAME = "report.json"
REPORT_PREFIX = "report/"
# Letters and digits: batch_by_deadline holds the words batch, by and deadline.
WORD_PATTERN = re.compile(r"[^\W_]+")


def words_of(text: str) -> list[str]:
    return WORD_PATTERN.findall(text.lower())


def run_directory(problem_root: Path, run_id: str | None) -> Path:
    """The named run, or the run whose status file (or directory) changed last."""
    runs_root = problem_root / RUNS_DIRECTORY
    runs = (
        [entry for entry in runs_root.iterdir() if entry.is_dir()]
        if runs_root.is_dir()
        else []
    )
    if run_id is not None and not (runs_root / run_id).is_dir():
        raise FileNotFoundError(f"no run {run_id} under {runs_root}")
    elif run_id is not None:
        return runs_root / run_id
    elif not runs:
        raise FileNotFoundError(f"no runs under {runs_root}; start one with vlah run")
    else:
        return max(
            runs,
            key=lambda run: (
                (run / STATUS_FILE_NAME).stat().st_mtime
                if (run / STATUS_FILE_NAME).is_file()
                else run.stat().st_mtime
            ),
        )


def run_status(directory: Path) -> RunStatusFile | None:
    """The run's status file; None for a run from before status files existed."""
    path = directory / STATUS_FILE_NAME
    return read_status(path) if path.is_file() else None


@dataclass(frozen=True, kw_only=True)
class BlackboardFiles:
    """The files on the blackboard's main branch, read without opening its store."""

    repository: Path

    @classmethod
    def of_run(cls, directory: Path) -> "BlackboardFiles":
        repository = directory / f"{BLACKBOARD_NAME}.git"
        if not (repository / "HEAD").is_file():
            raise FileNotFoundError(f"{directory} has no blackboard repository")
        else:
            return cls(repository=repository)

    def git(self, arguments: list[str], input_bytes: bytes | None) -> bytes:
        completed = subprocess.run(
            ["git", "--git-dir", str(self.repository), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(
                f"git {' '.join(arguments)} failed in {self.repository}: "
                f"{completed.stderr.decode(errors='replace').strip()}"
            )
        else:
            return completed.stdout

    def object_ids(self, prefix: str) -> dict[str, str]:
        """Path to blob id for each file under prefix."""
        listing = self.git(["ls-tree", "-r", "-z", MAIN_REF, "--", prefix], None)
        object_ids: dict[str, str] = {}
        for entry in listing.decode().split("\x00"):
            if entry:
                description, path = entry.split("\t", 1)
                object_ids[path] = description.split()[2]
            else:
                continue
        return object_ids

    def read_objects(self, object_ids: dict[str, str]) -> dict[str, bytes]:
        """Every listed file, in one git cat-file --batch run."""
        if not object_ids:
            return {}
        else:
            request = "".join(f"{object_id}\n" for object_id in object_ids.values())
            output = self.git(["cat-file", "--batch"], request.encode())
            contents: dict[str, bytes] = {}
            offset = 0
            for path in object_ids:
                header_end = output.index(b"\n", offset)
                size = int(output[offset:header_end].split()[2])
                contents[path] = output[header_end + 1 : header_end + 1 + size]
                offset = header_end + 1 + size + 1
            return contents

    def files(self, prefix: str) -> dict[str, bytes]:
        return self.read_objects(self.object_ids(prefix))

    def events_after(self, sequence: int) -> list[Event]:
        """The events merged after sequence, in order, each with the number it was given."""
        object_ids = self.object_ids("events/")
        pointer_ids = {
            path: object_id
            for path, object_id in object_ids.items()
            if path.startswith(SEQUENCE_POINTER_PREFIX)
            and int(path.removeprefix(SEQUENCE_POINTER_PREFIX)) > sequence
        }
        event_paths = {
            int(path.removeprefix(SEQUENCE_POINTER_PREFIX)): content.decode()
            for path, content in self.read_objects(pointer_ids).items()
        }
        event_files = self.read_objects(
            {path: object_ids[path] for path in event_paths.values()}
        )
        return [
            event_from_json(event_files[path].decode()).model_copy(
                update={"seq": event_sequence}
            )
            for event_sequence, path in sorted(event_paths.items())
        ]


class ClaimHit(FrozenModel):
    """A claim that holds every word of a search; status is its latest one."""

    claim_id: str
    type: ClaimType
    status: str
    approach: str
    author: str
    path: str
    word_count: int
    body: str


def search_claims(
    files: BlackboardFiles, query_words: set[str], limit: int
) -> list[ClaimHit]:
    """Claims with every query word, the most mentions first."""
    statuses = {
        event.target: event.to
        for event in files.events_after(0)
        if event.type == "claim_status"
        and event.target is not None
        and event.to is not None
    }
    hits: list[ClaimHit] = []
    claim_files = {
        path: content
        for path, content in files.files("claims/").items()
        if path.endswith(".md")
    }
    for path, content in sorted(claim_files.items()):
        claim = parse_claim(content.decode("utf-8", "replace"))
        words = Counter(
            words_of(
                " ".join(
                    [claim.id, claim.type, claim.approach, *claim.tags, claim.body]
                )
            )
        )
        if query_words <= set(words):
            hits.append(
                ClaimHit(
                    claim_id=claim.id,
                    type=claim.type,
                    status=statuses.get(claim.id, claim.status),
                    approach=claim.approach,
                    author=claim.author,
                    path=path,
                    word_count=sum(words[word] for word in query_words),
                    body=claim.body,
                )
            )
        else:
            continue
    return sorted(hits, key=lambda hit: -hit.word_count)[:limit]


class RunReport(FrozenModel):
    """The deliverable once the run has closed out, every baseline, and report/ files."""

    run_id: str
    deliverable: BaselineRecord | None
    baselines: tuple[BaselineRecord, ...]
    files: dict[str, str]


def run_report(directory: Path, files: BlackboardFiles) -> RunReport:
    baselines = tuple(
        sorted(
            (
                BaselineRecord.model_validate_json(content)
                for path, content in files.files("baseline/").items()
                if path.endswith(".json")
            ),
            key=lambda record: record.k,
        )
    )
    report_path = directory / REPORT_FILE_NAME
    document: object = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.is_file()
        else None
    )
    status = run_status(directory)
    deliverable_k = status.deliverable_baseline if status is not None else None
    if isinstance(document, dict) and "deliverable" in document:
        deliverable = BaselineRecord.model_validate(document["deliverable"])
    else:
        deliverable = next(
            (record for record in baselines if record.k == deliverable_k), None
        )
    return RunReport(
        run_id=directory.name,
        deliverable=deliverable,
        baselines=baselines,
        files={
            path: content.decode("utf-8", "replace")
            for path, content in sorted(files.files(REPORT_PREFIX).items())
        },
    )
