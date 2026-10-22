"""The start pack a worker session begins from: CONTEXT.md, composed by arm (D58, D77).

Every arm gets the task, spec.md and the charter's readable parts. Then:
- B0 (spec_notes): the agent's own claims, as its notes;
- B1 (spec_own_claims): the agent's own claims;
- S1 (spec_own_claims_scores): its own claims and the score table of every candidate;
- S2 (spec_digest_diff): the approach digest (empty without a Librarian) and the
  ranked diff since the agent last looked, truncated to diff_truncate_tokens.
"""

import json
from dataclasses import dataclass

import yaml

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.indexer.api import Indexer, WorkingSet
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.ports.executor import StartPack

CONTEXT_FILE = "CONTEXT.md"
CHARTER_FILES = ("charter/constraints.md", "charter/progress.md")
NO_CLAIMS_TEXT = "You have not written any claims yet."


@dataclass(frozen=True, kw_only=True)
class Section:
    heading: str
    body: str


@dataclass(frozen=True, kw_only=True)
class ContextSources:
    blackboard: Blackboard
    indexer: Indexer
    arm: ArmConfig
    params: Params


def render_context(sections: tuple[Section, ...]) -> str:
    return (
        "\n\n".join(
            f"## {section.heading}\n\n{section.body.strip()}" for section in sections
        )
        + "\n"
    )


def read_text(blackboard: Blackboard, path: str) -> str | None:
    if path in blackboard.list_paths(path, None):
        return blackboard.read(path, None).decode("utf-8", "replace")
    else:
        return None


def task_section(task: Task) -> Section:
    lines = [
        f"Task {task.id} ({task.kind}) on approach {task.approach}.",
        "",
        task.goal,
    ]
    if task.refs:
        lines.extend(["", f"It is about: {', '.join(task.refs)}."])
    else:
        pass
    return Section(heading="Your task", body="\n".join(lines))


def problem_sections(blackboard: Blackboard) -> tuple[Section, ...]:
    sections = [Section(heading="spec.md", body=read_text(blackboard, "spec.md") or "")]
    for path in CHARTER_FILES:
        text = read_text(blackboard, path)
        if text is not None:
            sections.append(Section(heading=path, body=text))
        else:
            pass
    return tuple(sections)


def own_claims_section(blackboard: Blackboard, agent: ActorId, heading: str) -> Section:
    paths = blackboard.list_paths(f"claims/{agent}/", None)
    texts = [
        f"### {path}\n\n{blackboard.read(path, None).decode('utf-8', 'replace').strip()}"
        for path in paths
    ]
    return Section(heading=heading, body="\n\n".join(texts) or NO_CLAIMS_TEXT)


def score_rows(blackboard: Blackboard) -> list[str]:
    """One row per measured level of every candidate with a result."""
    rows: list[str] = []
    for path in blackboard.list_paths("candidates/", None):
        if not path.endswith("/result.json"):
            continue
        else:
            result: object = json.loads(blackboard.read(path, None))
            manifest_path = path.removesuffix("result.json") + "manifest.yaml"
            manifest: object = yaml.safe_load(blackboard.read(manifest_path, None))
            approach = manifest.get("approach") if isinstance(manifest, dict) else None
            levels = result.get("levels") if isinstance(result, dict) else None
            candidate = path.split("/")[1]
            for level in levels if isinstance(levels, list) else []:
                if isinstance(level, dict):
                    rows.append(
                        f"| {candidate[:12]} | {approach} | {level.get('level')} "
                        f"| {level.get('score')} | {level.get('n')} | {level.get('status')} |"
                    )
                else:
                    pass
    return rows


def score_table_section(blackboard: Blackboard) -> Section:
    rows = score_rows(blackboard)
    if rows:
        header = "| candidate | approach | level | score | n | status |\n|---|---|---|---|---|---|"
        body = "\n".join([header, *rows])
    else:
        body = "No candidate has a result yet."
    return Section(heading="Every candidate's scores", body=body)


def digest_section(blackboard: Blackboard, approach: str) -> Section:
    digest = read_text(blackboard, f"digests/{approach}.md")
    return Section(
        heading=f"Digest of {approach}",
        body=digest or "No digest has been written yet.",
    )


def build_start_pack(
    agent: ActorId, task: Task, sources: ContextSources, cursor: int
) -> tuple[StartPack, int]:
    """The start pack, and the cursor to use next time (it moves only in S2)."""
    blackboard = sources.blackboard
    sections = [task_section(task), *problem_sections(blackboard)]
    composition = sources.arm.context_composition
    next_cursor = cursor
    if composition == "spec_notes":
        sections.append(own_claims_section(blackboard, agent, "Your notes"))
    elif composition == "spec_own_claims":
        sections.append(own_claims_section(blackboard, agent, "Your claims"))
    elif composition == "spec_own_claims_scores":
        sections.append(own_claims_section(blackboard, agent, "Your claims"))
        sections.append(score_table_section(blackboard))
    else:
        diff = sources.indexer.diff_since(
            cursor,
            WorkingSet(approach=task.approach, target_claims=task.refs, recent_ids=()),
            agent,
            sources.params.diff_truncate_tokens,
            sources.arm,
        )
        next_cursor = diff.cursor
        sections.append(digest_section(blackboard, task.approach))
        sections.append(
            Section(
                heading="What changed since you last looked",
                body=diff.rendered or "Nothing new.",
            )
        )
    return StartPack(files={CONTEXT_FILE: render_context(tuple(sections))}), next_cursor
