"""The command judge: a SignalSource that runs a shell command on a candidate.

The command from herd.yaml runs with the candidate's payload directory as its
working directory and these placeholders and variables filled in:

    {candidate_dir} / VLAH_CANDIDATE_DIR   the payload directory
    {problem_root}  / VLAH_PROBLEM_ROOT    the problem folder (judge scripts, data)
    {dataset}       / VLAH_DATASET         dev, audit or judge (D72)
    {baseline}      / VLAH_BASELINE        the baseline id the candidate targets
    VLAH_CANDIDATE_HASH, VLAH_SOURCE, VLAH_ACTOR

It prints one JSON object as its last line of output: {"score": 0.63} at least,
with "per_seed": [...] when it scores several items or seeds (the paired
comparison and the Integrator's remeasure need them), "n", "ci",
"constraint_ok": false when a hard constraint is broken, and anything else under
"extra". A non-zero exit or unparsable output is a void run (score null).

Each run happens on its own thread, so submit returns at once and poll answers
when the command has finished. A repeat of a candidate on the same dataset is
answered from the first run and marked is_cached (TB section 11.3), so the eval
client charges nothing for it.
"""

import logging
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue

from vlah.core.blackboard.api import Blackboard
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import ScoreResult, SourceDecl
from vlah.ports.signal_source import CandidateRef, Dataset

logger = logging.getLogger(__name__)

BASELINE_ZERO = "baseline@0"
DEFAULT_TIMEOUT_SECONDS = 3600.0
OUTPUT_TAIL_CHARACTERS = 2000


class JudgeOutput(BaseModel):
    """The JSON line a command judge prints; unknown fields are kept under extra."""

    model_config = ConfigDict(extra="allow", frozen=True)

    score: float | None
    per_seed: tuple[float, ...] = ()
    n: int | None = None
    ci: tuple[float, float] | None = None
    constraint_ok: bool | None = None

    def as_score(self) -> ScoreResult:
        per_seed = (
            self.per_seed
            if self.per_seed
            else (() if self.score is None else (self.score,))
        )
        extra: dict[str, JsonValue] = {
            str(key): value for key, value in (self.model_extra or {}).items()
        }
        return ScoreResult(
            value=self.score,
            n=self.n if self.n is not None else max(1, len(per_seed)),
            ci=self.ci,
            per_seed=per_seed,
            constraint_ok=self.constraint_ok,
            extra=extra,
        )


def void_score(reason: str) -> ScoreResult:
    return ScoreResult(value=None, n=1, extra={"error": reason})


def parse_judge_output(stdout: str) -> ScoreResult:
    """The last non-empty line of stdout is the judge's JSON."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return void_score("the judge printed nothing")
    else:
        try:
            return JudgeOutput.model_validate_json(lines[-1]).as_score()
        except ValueError as error:
            return void_score(f"the judge's last line is not a score: {error}")


@dataclass(frozen=True, kw_only=True)
class JudgeRun:
    """What one command run was asked to score."""

    candidate_directory: Path
    candidate_hash: str
    actor: ActorId
    baseline: str
    dataset: Dataset


@dataclass(kw_only=True)
class CommandJudge:
    """SignalSource over a shell command (quickstart section 4)."""

    source_declaration: SourceDecl
    command: str
    problem_root: Path
    clock: Clock
    environment: dict[str, str] = field(default_factory=dict[str, str])
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    log_directory: Path | None = None
    results: dict[str, ScoreResult] = field(default_factory=dict[str, ScoreResult])
    first_job_by_key: dict[tuple[str, Dataset], str] = field(
        default_factory=dict[tuple[str, Dataset], str]
    )
    threads: dict[str, threading.Thread] = field(
        default_factory=dict[str, threading.Thread]
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    job_count: int = 0

    @property
    def declaration(self) -> SourceDecl:
        return self.source_declaration

    def shell_command(self, run: JudgeRun) -> str:
        return self.command.format(
            candidate_dir=shlex.quote(str(run.candidate_directory)),
            problem_root=shlex.quote(str(self.problem_root)),
            dataset=run.dataset,
            baseline=run.baseline,
        )

    def run_environment(self, run: JudgeRun) -> dict[str, str]:
        return {
            **os.environ,
            **self.environment,
            "VLAH_CANDIDATE_DIR": str(run.candidate_directory),
            "VLAH_PROBLEM_ROOT": str(self.problem_root),
            "VLAH_DATASET": run.dataset,
            "VLAH_BASELINE": run.baseline,
            "VLAH_CANDIDATE_HASH": run.candidate_hash,
            "VLAH_SOURCE": self.source_declaration.id,
            "VLAH_ACTOR": run.actor,
        }

    def measure(self, run: JudgeRun) -> ScoreResult:
        """Run the command to its end and parse its score; never raises."""
        try:
            completed = subprocess.run(
                self.shell_command(run),
                shell=True,
                cwd=run.candidate_directory,
                env=self.run_environment(run),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return void_score(f"the judge ran past {self.timeout_seconds:g} s")
        except OSError as error:
            return void_score(f"the judge could not start: {error}")
        self.log(run, completed.stdout, completed.stderr)
        if completed.returncode != 0:
            return void_score(
                f"the judge exited with {completed.returncode}: "
                f"{completed.stderr[-OUTPUT_TAIL_CHARACTERS:]}"
            )
        else:
            return parse_judge_output(completed.stdout)

    def log(self, run: JudgeRun, stdout: str, stderr: str) -> None:
        if self.log_directory is None:
            return
        else:
            self.log_directory.mkdir(parents=True, exist_ok=True)
            name = (
                f"{self.source_declaration.id}-{run.dataset}-{run.candidate_hash[:12]}"
            )
            (self.log_directory / f"{name}.out").write_text(stdout, encoding="utf-8")
            (self.log_directory / f"{name}.err").write_text(stderr, encoding="utf-8")

    def measure_directory(
        self, directory: Path, dataset: Dataset, baseline: str = BASELINE_ZERO
    ) -> ScoreResult:
        """Score a directory now, outside the queue: the baseline's calibration."""
        return self.measure(
            JudgeRun(
                candidate_directory=directory,
                candidate_hash="baseline",
                actor=ActorId("integrator"),
                baseline=baseline,
                dataset=dataset,
            )
        )

    def submit(
        self, candidate: CandidateRef, actor: ActorId, baseline: str, dataset: Dataset
    ) -> str:
        with self.lock:
            self.job_count += 1
            job_id = f"{self.source_declaration.id}-{self.job_count:05d}"
            key = (candidate.candidate_hash, dataset)
            first_job_id = self.first_job_by_key.setdefault(key, job_id)
        if first_job_id != job_id:
            # A repeat: the first run's result, marked cached, once it is in.
            self.threads[job_id] = self.threads[first_job_id]
            with self.lock:
                self.first_job_by_key[(job_id, dataset)] = first_job_id
            return job_id
        else:
            run = JudgeRun(
                candidate_directory=candidate.payload_directory,
                candidate_hash=candidate.candidate_hash,
                actor=actor,
                baseline=baseline,
                dataset=dataset,
            )
            thread = threading.Thread(
                target=self.run_job, args=(job_id, run), name=job_id, daemon=True
            )
            self.threads[job_id] = thread
            thread.start()
            return job_id

    def run_job(self, job_id: str, run: JudgeRun) -> None:
        result = self.measure(run)
        with self.lock:
            self.results[job_id] = result
        logger.info(
            f"{self.source_declaration.id} scored {run.candidate_hash[:12]} on "
            f"{run.dataset}: {result.value}"
        )

    def poll(self, job_id: str) -> ScoreResult | None:
        with self.lock:
            if job_id in self.results:
                return self.results[job_id]
            else:
                first_job_id = next(
                    (
                        first
                        for (job, dataset), first in self.first_job_by_key.items()
                        if job == job_id and first != job_id
                    ),
                    None,
                )
                first_result = (
                    self.results.get(first_job_id) if first_job_id is not None else None
                )
        if first_result is None:
            return None
        else:
            return first_result.model_copy(update={"is_cached": True})


@dataclass(kw_only=True)
class CommandBaselineScores:
    """baseline@0 measured once per source and dataset; later baselines from their records."""

    judges: dict[str, CommandJudge]
    baseline_directory: Path
    blackboard: Blackboard
    measured: dict[tuple[str, Dataset], tuple[float, ...] | None] = field(
        default_factory=dict[tuple[str, Dataset], tuple[float, ...] | None]
    )
    lock: threading.Lock = field(default_factory=threading.Lock)

    def baseline_zero(
        self, source_id: str, dataset: Dataset
    ) -> tuple[float, ...] | None:
        key = (source_id, dataset)
        with self.lock:
            if key in self.measured:
                return self.measured[key]
            else:
                pass
            result = self.judges[source_id].measure_directory(
                self.baseline_directory, dataset
            )
            per_seed = result.per_seed if result.value is not None else None
            if per_seed is None:
                logger.error(
                    f"{source_id} could not score baseline@0 on {dataset}: "
                    f"{result.extra.get('error')}"
                )
            else:
                pass
            self.measured[key] = per_seed
            return per_seed

    def __call__(
        self, source_id: str, baseline: str, dataset: Dataset
    ) -> tuple[float, ...] | None:
        if baseline == BASELINE_ZERO:
            return self.baseline_zero(source_id, dataset)
        elif dataset != "dev":
            return None
        else:
            record_path = f"baseline/{baseline.removeprefix('baseline@')}.json"
            if not self.blackboard.list_paths(record_path, None):
                return None
            else:
                record = BaselineRecord.model_validate_json(
                    self.blackboard.read(record_path, None)
                )
                return record.per_seed or None
