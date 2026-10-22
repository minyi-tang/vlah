"""The compose pool: compose tasks run outside every arm (D74, TB§11.8.1 step 2).

A compose task asks for one candidate that combines two verified candidates. It is
run by a fresh agent that belongs to no arm's worker pool and carries no arm's
context: its start pack is the task, spec.md and the two candidates' files, the
same in every arm. Its output goes only to the Integrator.
"""

from dataclasses import dataclass

from vlah.core.blackboard.api import Blackboard
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.core.worker.start_pack import (
    CONTEXT_FILE,
    Section,
    read_text,
    render_context,
    task_section,
)
from vlah.ports.executor import StartPack

COMPOSE_AGENT = ActorId("worker-00")


def candidate_section(blackboard: Blackboard, candidate_hash: str) -> Section:
    prefix = f"candidates/{candidate_hash}/"
    texts = [
        f"### {path}\n\n```\n{blackboard.read(path, None).decode('utf-8', 'replace')}\n```"
        for path in blackboard.list_paths(prefix, None)
    ]
    return Section(
        heading=f"Candidate {candidate_hash}",
        body="\n\n".join(texts) or "This candidate has no files on the blackboard.",
    )


@dataclass(frozen=True, kw_only=True)
class ComposeStartPacks:
    """The arm-free start pack of a compose task; the cursor never moves."""

    blackboard: Blackboard

    def __call__(
        self, agent: ActorId, task: Task, cursor: int
    ) -> tuple[StartPack, int]:
        sections = [
            task_section(task),
            Section(
                heading="spec.md", body=read_text(self.blackboard, "spec.md") or ""
            ),
            *(candidate_section(self.blackboard, ref) for ref in task.refs),
        ]
        return StartPack(files={CONTEXT_FILE: render_context(tuple(sections))}), cursor
