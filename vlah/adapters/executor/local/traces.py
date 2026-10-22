"""What the executor writes when a session ends: the trace, its pointer, a stub claim (D33, D77).

Every process that ends uploads stdout, stderr and the harness transcript to the
object store and writes a pointer file to the blackboard as the agent:
traces/<agent>/<task>.ptr for the task's first session, then
traces/<agent>/<task>.<n>.ptr, because files are append-only and one task can
run several sessions. When the task has no claim, a stub dead end tagged "stub"
cites that pointer, in the same commit so its evidence resolves.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from vlah.core.blackboard.api import Blackboard, WriteReceipt
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId, format_claim_id
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.states import INITIAL_STATUS
from vlah.core.types.tasks import Task
from vlah.ports.executor import ExitInfo
from vlah.ports.store import ObjectStore

logger = logging.getLogger(__name__)

STUB_TAG = "stub"
CONTEXT_FILE = "CONTEXT.md"
WRITE_ATTEMPTS = 3
RESTART_HINT = (
    "Your previous session for this task ended without writing a claim. You must "
    "write at least one claim with evidence before you finish; a dead_end saying "
    "'no finding this time' is allowed."
)


@dataclass(frozen=True, kw_only=True)
class SessionLogs:
    stdout: Path
    stderr: Path
    transcript: Path


@dataclass(frozen=True, kw_only=True)
class EndedSession:
    agent: ActorId
    task: Task
    handle_id: str
    attempt: int
    exit_info: ExitInfo
    reason: str
    logs: SessionLogs


def describe_exit(exit_info: ExitInfo) -> str:
    if exit_info.was_killed:
        return "killed by a signal"
    else:
        return f"exited with code {exit_info.return_code}"


def with_notice(files: dict[str, str], heading: str, text: str) -> dict[str, str]:
    """The start files with a section appended to CONTEXT.md, which every harness reads."""
    context = files.get(CONTEXT_FILE, "")
    separator = "\n\n" if context and not context.endswith("\n\n") else ""
    return {**files, CONTEXT_FILE: f"{context}{separator}## {heading}\n\n{text}\n"}


def interrupt_notice(interrupt: Interrupt) -> str:
    return (
        f"The herd stopped your previous session with a {interrupt.kind} interrupt "
        f"(rule {interrupt.rule}, id {interrupt.id}): {interrupt.reason}\n\n"
        "Act on it before you continue the task."
    )


def pointer_path(blackboard: Blackboard, agent: ActorId, task_id: str) -> str:
    """The next free pointer name for this task's sessions."""
    existing = blackboard.list_paths(f"traces/{agent}/{task_id}.", None)
    pointer_count = sum(1 for path in existing if path.endswith(".ptr"))
    if pointer_count == 0:
        return f"traces/{agent}/{task_id}.ptr"
    else:
        return f"traces/{agent}/{task_id}.{pointer_count + 1}.ptr"


def upload_logs(objects: ObjectStore, ended: EndedSession) -> dict[str, str]:
    """Archive every log that exists; traces are never deleted (D33)."""
    prefix = f"traces/{ended.agent}/{ended.task.id}/{ended.handle_id}/{ended.attempt}"
    pointers: dict[str, str] = {}
    for name, path in (
        ("stdout", ended.logs.stdout),
        ("stderr", ended.logs.stderr),
        ("transcript", ended.logs.transcript),
    ):
        if path.is_file():
            pointers[name] = objects.put(f"{prefix}/{name}", path.read_bytes(), False)
        else:
            pass
    return pointers


def pointer_file(ended: EndedSession, object_pointers: dict[str, str]) -> bytes:
    record = {
        "agent": ended.agent,
        "task": ended.task.id,
        "handle": ended.handle_id,
        "attempt": ended.attempt,
        "return_code": ended.exit_info.return_code,
        "was_killed": ended.exit_info.was_killed,
        "summary": f"Session {describe_exit(ended.exit_info)}: {ended.reason}.",
        "surprise": False,
        "objects": object_pointers,
    }
    return yaml.safe_dump(record, sort_keys=False).encode("utf-8")


def next_claim_number(blackboard: Blackboard, agent: ActorId) -> int:
    prefix = f"c-{agent}-"
    numbers = [
        int(name.removeprefix(prefix).removesuffix(".md"))
        for path in blackboard.list_paths(f"claims/{agent}/", None)
        if (name := Path(path).name).startswith(prefix)
        and name.removeprefix(prefix).removesuffix(".md").isdigit()
    ]
    return max(numbers, default=0) + 1


def stub_claim(ended: EndedSession, claim_number: int, trace_pointer: str) -> Claim:
    return Claim(
        id=format_claim_id(ended.agent, claim_number),
        type="dead_end",
        status=INITIAL_STATUS["dead_end"],
        approach=ended.task.approach,
        author=ended.agent,
        tags=(STUB_TAG,),
        evidence=(EvidenceRef(trace=trace_pointer),),
        body=(
            f"Stub written by the executor: the session for task {ended.task.id} "
            f"{describe_exit(ended.exit_info)} ({ended.reason}) without writing a "
            "claim. The trace is the evidence."
        ),
    )


def write_trace(
    blackboard: Blackboard,
    objects: ObjectStore,
    ended: EndedSession,
    needs_stub: bool,
) -> WriteReceipt:
    """Upload the logs and write the pointer, plus a stub claim when the task has none.

    Retries when another write takes the same name first, such as a claim from a
    tool call that was still in flight when the process died.
    """
    object_pointers = upload_logs(objects, ended)
    receipt: WriteReceipt | None = None
    for _ in range(WRITE_ATTEMPTS):
        trace_pointer = pointer_path(blackboard, ended.agent, ended.task.id)
        files = {trace_pointer: pointer_file(ended, object_pointers)}
        if needs_stub:
            claim = stub_claim(
                ended, next_claim_number(blackboard, ended.agent), trace_pointer
            )
            files[f"claims/{ended.agent}/{claim.id}.md"] = render_claim(claim).encode(
                "utf-8"
            )
        else:
            pass
        receipt = blackboard.write(
            ended.agent, files, f"trace of {ended.task.id} by {ended.agent}"
        )
        if receipt.status != "rejected":
            return receipt
        elif all(violation.rule == "file_exists" for violation in receipt.violations):
            continue
        else:
            break
    assert receipt is not None
    logger.error(
        f"could not write the trace of {ended.task.id} for {ended.agent}: "
        f"{[violation.message for violation in receipt.violations]}"
    )
    return receipt
