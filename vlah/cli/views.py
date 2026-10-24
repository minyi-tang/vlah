"""vlah status, events, search, report, inbox, stop and demo (quickstart sections 2, 6 and 7).

Each view reads only the run's directory and its blackboard (vlah.cli.run_files),
so it works on a live run and on a finished one. It prints text, or JSON with
--json: the status file as written, events as one JSON object per line, and the
other views as one JSON document.
"""

import json
import statistics
import tempfile
import textwrap
import time
from pathlib import Path
from typing import TextIO

from vlah.adapters.human.cli.human import CliHuman
from vlah.cli.run_files import (
    REPORT_FILE_NAME,
    BlackboardFiles,
    RunReport,
    run_directory,
    run_report,
    run_status,
    search_claims,
    words_of,
)
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import (
    AuditChecked,
    BaselineAdvanced,
    BaselineReverted,
    DedupHit,
    Event,
    EventType,
    IntegrationFailed,
    InteractionFound,
    MapUpdated,
    RemeasureReady,
    ResultReady,
    Stopped,
    event_to_json,
)
from vlah.core.types.sources import Paired
from vlah.ports.human import HumanMessage
from vlah.runtime.demo import DEMO_PROBLEM_NAME, DEMO_WORKER_COUNT, DIRECTIONS, demo_run
from vlah.runtime.status import RunStatusFile, WorkerStatus
from vlah.runtime.supervisor import STOP_REQUEST_FILE_NAME

FOLLOW_POLL_SECONDS = 2.0
HASH_PREFIX_LENGTH = 12
GOAL_WIDTH = 100
BODY_WIDTH = 160
DEMO_EVENT_TYPES: tuple[EventType, ...] = (
    "result_ready",
    "dedup_hit",
    "baseline_advanced",
    "baseline_reverted",
    "integration_failed",
    "escalation",
    "stopped",
)


def clock_text(timestamp_seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp_seconds))


def duration_text(seconds: float) -> str:
    whole_seconds = int(seconds)
    hours, minutes = whole_seconds // 3600, whole_seconds % 3600 // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    elif minutes:
        return f"{minutes}m{whole_seconds % 60:02d}s"
    else:
        return f"{whole_seconds}s"


def short_hash(candidate_hash: str) -> str:
    return candidate_hash[:HASH_PREFIX_LENGTH]


def gain_text(paired: Paired | None) -> str:
    return "void" if paired is None else f"{paired.mean:+.3g} ± {paired.se:.2g}"


def score_text(per_seed: tuple[float, ...]) -> str:
    return f"{statistics.fmean(per_seed):.4g}" if per_seed else "not measured"


def event_details(event: Event) -> str:
    data = event.data
    if isinstance(data, ResultReady):
        score = "void" if data.score is None else f"{data.score:.4g}"
        return (
            f"{data.claim_id} {short_hash(data.candidate)} by {data.submitter} at "
            f"{data.level}: score {score}, gain {gain_text(data.paired)}"
        )
    elif isinstance(data, RemeasureReady):
        return (
            f"{short_hash(data.candidate)} on {data.baseline} ({data.dataset}): "
            f"gain {gain_text(data.paired)}"
        )
    elif isinstance(data, BaselineAdvanced):
        return (
            f"baseline@{data.baseline} = baseline@{data.parent_baseline} + "
            f"{short_hash(data.candidate)}, gain {gain_text(data.paired)}"
        )
    elif isinstance(data, BaselineReverted):
        removed = ", ".join(short_hash(candidate) for candidate in data.removed)
        return (
            f"baseline@{data.new_baseline} drops {removed} from "
            f"baseline@{data.from_baseline}: {data.reason}"
        )
    elif isinstance(data, IntegrationFailed):
        return (
            f"{short_hash(data.candidate)} on baseline@{data.baseline}: {data.reason}"
        )
    elif isinstance(data, InteractionFound):
        return (
            f"{short_hash(data.candidate)} on baseline@{data.baseline}: "
            f"{gain_text(data.earlier)} became {gain_text(data.remeasured)}"
        )
    elif isinstance(data, AuditChecked):
        flag = ", flagged" if data.flagged else ""
        return (
            f"baselines {data.segment[0]} to {data.segment[1]}: daily "
            f"{gain_text(data.daily)}, audit {gain_text(data.audit)}{flag}"
        )
    elif isinstance(data, MapUpdated):
        return f"map measured on baseline@{data.baseline}"
    elif isinstance(data, DedupHit):
        return f"{short_hash(data.candidate)} repeats {data.original_claim}"
    elif isinstance(data, Stopped):
        return data.reason if event.reason is None else f"{data.reason}: {event.reason}"
    else:
        change = f"{event.from_ or '?'} -> {event.to}" if event.to is not None else None
        return "  ".join(
            part for part in (event.target, change, event.reason) if part is not None
        )


def event_line(event: Event, time_text: str) -> str:
    return (
        f"{event.seq or 0:>5}  {time_text}  {event.actor:<14} {event.type:<18} "
        f"{event_details(event)}"
    ).rstrip()


def worker_lines(worker: WorkerStatus, now_seconds: float) -> list[str]:
    last_write = (
        ""
        if worker.last_write_ts is None
        else f", last write {duration_text(now_seconds - worker.last_write_ts)} ago"
    )
    crashes = f", {worker.crash_count} crash(es)" if worker.crash_count else ""
    history = f"{worker.session_count} session(s){crashes}{last_write}"
    task = worker.task
    if task is None:
        return [f"{worker.agent:<10} idle; {history}"]
    else:
        lease_age = duration_text(worker.lease_age_seconds or 0.0)
        heartbeat = (
            "no heartbeat yet"
            if worker.last_heartbeat_ts is None
            else f"heartbeat {duration_text(now_seconds - worker.last_heartbeat_ts)} ago"
        )
        goal = textwrap.shorten(task.goal, GOAL_WIDTH, placeholder=" ...")
        return [
            f"{worker.agent:<10} {task.id} {task.kind} on {task.approach}, lease "
            f"{lease_age}, {heartbeat}; {history}",
            f"{'':<10} {goal}",
        ]


def status_text(status: RunStatusFile) -> str:
    now_seconds = status.written_ts
    baseline = status.baseline
    judge = status.judge
    score = "not measured" if baseline.score is None else f"{baseline.score:.4g}"
    lines = [
        f"run {status.run_id} ({status.arm}): {status.phase}, as of "
        f"{clock_text(now_seconds)}",
        f"started {clock_text(status.started_ts)}; time limit "
        f"{clock_text(status.deadline_ts)}"
        + (
            f", {duration_text(max(status.deadline_ts - now_seconds, 0.0))} left"
            if status.phase == "running"
            else ""
        ),
        *([f"stop reason: {status.stop_reason}"] if status.stop_reason else []),
        *(
            [f"closeout failed: {status.closeout_error}"]
            if status.closeout_error
            else []
        ),
        *(
            [f"deliverable: baseline@{status.deliverable_baseline}"]
            if status.deliverable_baseline is not None
            else []
        ),
        f"baseline@{baseline.k}: score {score}, {baseline.candidate_count} merged "
        f"candidate(s){', guard pending' if baseline.guard_pending else ''}",
        f"judge: {judge.runs_used} run(s), {judge.machine_hours_used:.2f} of "
        f"{judge.machine_hours_limit:g} machine-hours",
        *(
            f"  {source.source}: {source.runs_used}"
            f"{'' if source.run_cap is None else f' of {source.run_cap}'} run(s), "
            f"at most {source.per_day_cap} a day, "
            f"{source.machine_hours_used:.2f} machine-hours"
            for source in judge.sources
        ),
        f"queues: {status.queues.arm_waiting} task(s) waiting, "
        f"{status.queues.compose_waiting} compose task(s)",
        "workers:",
        *(
            f"  {line}"
            for worker in status.workers
            for line in worker_lines(worker, now_seconds)
        ),
        "recent events:",
        *(
            f"  {event_line(event, clock_text(event.ts))}"
            for event in status.recent_events
        ),
    ]
    return "\n".join(lines)


def show_status(
    problem_root: Path, run_id: str | None, as_json: bool, output: TextIO
) -> None:
    directory = run_directory(problem_root, run_id)
    status = run_status(directory)
    if status is None:
        raise FileNotFoundError(
            f"{directory} has no status file; the run started before vlah wrote one"
        )
    elif as_json:
        print(status.model_dump_json(indent=2), file=output)
    else:
        print(status_text(status), file=output)


def show_events(
    problem_root: Path,
    run_id: str | None,
    types: tuple[EventType, ...],
    is_following: bool,
    as_json: bool,
    output: TextIO,
) -> None:
    """Print the merged events; with is_following, keep printing until the run finishes."""
    directory = run_directory(problem_root, run_id)
    files = BlackboardFiles.of_run(directory)
    last_sequence = 0
    try:
        while True:
            # Read the phase first: events merged before the run finished are
            # all on main by the time it says finished.
            status = run_status(directory)
            is_finished = (
                status.phase == "finished"
                if status is not None
                else (directory / REPORT_FILE_NAME).is_file()
            )
            events = files.events_after(last_sequence)
            for event in events:
                if types and event.type not in types:
                    continue
                elif as_json:
                    print(event_to_json(event), file=output, flush=True)
                else:
                    print(
                        event_line(event, clock_text(event.ts)), file=output, flush=True
                    )
            last_sequence = max(
                (event.seq or 0 for event in events), default=last_sequence
            )
            if not is_following or is_finished:
                return
            else:
                time.sleep(FOLLOW_POLL_SECONDS)
    except KeyboardInterrupt:
        return


def show_search(
    problem_root: Path,
    run_id: str | None,
    query: str,
    limit: int,
    as_json: bool,
    output: TextIO,
) -> None:
    query_words = set(words_of(query))
    if not query_words:
        raise ValueError(f"the query {query!r} has no words to look for")
    else:
        pass
    directory = run_directory(problem_root, run_id)
    hits = search_claims(BlackboardFiles.of_run(directory), query_words, limit)
    if as_json:
        print(
            json.dumps([hit.model_dump(mode="json") for hit in hits], indent=2),
            file=output,
        )
    elif not hits:
        print(
            f"No claim in {directory.name} holds every word of {query!r}.", file=output
        )
    else:
        for hit in hits:
            print(
                f"{hit.claim_id}  {hit.type}  {hit.status}  {hit.approach}  {hit.author}",
                file=output,
            )
            print(
                f"    {textwrap.shorten(hit.body, BODY_WIDTH, placeholder=' ...')}",
                file=output,
            )


def baseline_line(record: BaselineRecord) -> str:
    added = (
        ""
        if record.added is None
        else f"  + {short_hash(record.added)}, gain {gain_text(record.paired)}"
    )
    removed = (
        f"  - {', '.join(short_hash(candidate) for candidate in record.removed)}"
        if record.removed
        else ""
    )
    pending = "  (guard pending)" if record.guard_pending else ""
    return f"baseline@{record.k}  score {score_text(record.per_seed)}{added}{removed}{pending}"


def report_text(report: RunReport, directory: Path) -> str:
    deliverable = report.deliverable
    lines = [
        f"run {report.run_id}",
        *(
            ["deliverable: none yet; the run has not closed out"]
            if deliverable is None
            else [
                f"deliverable: baseline@{deliverable.k}, score "
                f"{score_text(deliverable.per_seed)}, {len(deliverable.candidates)} "
                "merged candidate(s)",
                *(f"  {candidate}" for candidate in deliverable.candidates),
            ]
        ),
        "baselines:",
        *(f"  {baseline_line(record)}" for record in report.baselines),
        *(
            []
            if report.files
            else [
                "report/: none on the blackboard; the Integrator writes it at closeout"
            ]
        ),
    ]
    for path, file_text in report.files.items():
        copy = directory / path
        if path.endswith(".md"):
            lines.extend([f"{path}:", textwrap.indent(file_text.rstrip(), "  ")])
        else:
            where = f", copied to {copy}" if copy.is_file() else ""
            lines.append(f"{path}: {len(file_text.splitlines())} lines{where}")
    if not (directory / REPORT_FILE_NAME).is_file():
        lines.append(
            f"{REPORT_FILE_NAME}: not written yet; vlah run writes it when the run ends"
        )
    else:
        pass
    return "\n".join(lines)


def show_report(
    problem_root: Path, run_id: str | None, as_json: bool, output: TextIO
) -> None:
    directory = run_directory(problem_root, run_id)
    report = run_report(directory, BlackboardFiles.of_run(directory))
    if as_json:
        print(report.model_dump_json(indent=2), file=output)
    else:
        print(report_text(report, directory), file=output)


def show_inbox(
    problem_root: Path,
    run_id: str | None,
    as_json: bool,
    human: CliHuman,
) -> None:
    """Open escalations: every escalation event, since answers come in Wave 3."""
    directory = run_directory(problem_root, run_id)
    escalations = [
        event
        for event in BlackboardFiles.of_run(directory).events_after(0)
        if event.type == "escalation"
    ]
    if as_json:
        print(
            json.dumps(
                [json.loads(event_to_json(event)) for event in escalations], indent=2
            ),
            file=human.output,
        )
    elif not escalations:
        print(f"No questions are waiting in {directory.name}.", file=human.output)
    else:
        for event in escalations:
            evidence = (
                f"\nLook at: {', '.join(event.evidence)}" if event.evidence else ""
            )
            human.send(
                HumanMessage(
                    sender="herd",
                    text=(
                        f"{event.local_id} (event {event.seq}) {event.target or ''}\n"
                        f"{event.reason or ''}{evidence}"
                    ),
                    timestamp_seconds=event.ts,
                )
            )


def stop_run(
    problem_root: Path, run_id: str | None, clock: Clock, output: TextIO
) -> None:
    """Write the stop request the run's supervisor picks up on its next tick."""
    directory = run_directory(problem_root, run_id)
    status = run_status(directory)
    if status is not None and status.phase != "running":
        raise ValueError(f"run {directory.name} is already {status.phase}")
    else:
        (directory / STOP_REQUEST_FILE_NAME).write_text(
            json.dumps(
                {"requested_by": "vlah stop", "requested_ts": clock.now_seconds()}
            ),
            encoding="utf-8",
        )
        print(
            f"Asked run {directory.name} to stop. Its supervisor closes it out on its "
            f"next tick and keeps everything learned; watch it with: vlah status "
            f"{problem_root}",
            file=output,
        )


def show_demo(seed: int, output: TextIO) -> None:
    """The demo's story: what the judge saw, what merged, and the result."""
    with tempfile.TemporaryDirectory(prefix="vlah-demo-") as directory:
        run = demo_run(seed, Path(directory))
        report = run.report()
        events = list(run.events())
        claims = [
            parse_claim(run.components.blackboard.read(path, None).decode())
            for path in run.components.blackboard.list_paths("claims/", None)
        ]
    judge_runs = run.fanout.judge_runs
    print(
        f"demo  {DEMO_PROBLEM_NAME} · {DEMO_WORKER_COUNT} workers · "
        f"{len(DIRECTIONS)} directions · seed {seed}\n",
        file=output,
    )
    for event in events:
        if event.type in DEMO_EVENT_TYPES:
            elapsed = f"{int(event.ts // 3600):02d}:{int(event.ts % 3600 // 60):02d}"
            print(f"  [{elapsed}] {event.type:<18} {event_details(event)}", file=output)
        else:
            continue
    baseline_zero = report.baselines[0]
    print(
        f"\n  baseline {score_text(baseline_zero.per_seed)} -> "
        f"{score_text(report.deliverable.per_seed)} (baseline@{report.deliverable.k}) · "
        f"{judge_runs} judge runs · "
        f"{sum(1 for event in events if event.type == 'dedup_hit')} duplicate(s) "
        f"skipped · {sum(1 for claim in claims if claim.type == 'dead_end')} dead "
        "end(s)",
        file=output,
    )
