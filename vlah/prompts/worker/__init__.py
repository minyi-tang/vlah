"""Shared worker mission, required boundaries and tool protocols, and suggestions."""

from importlib.resources import files
from string import Template

from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task

PARTS = (
    ("Goal", "goal.md"),
    ("Boundaries and tool protocols", "rules.md"),
    ("Suggestions", "suggestions.md"),
)


def render_worker_prompt(agent: ActorId, task: Task) -> str:
    templates = files(__name__)
    rendered = [
        f"# {heading}\n\n"
        + Template(templates.joinpath(name).read_text(encoding="utf-8"))
        .substitute(agent=agent, goal=task.goal)
        .strip()
        for heading, name in PARTS
    ]
    return "\n\n".join(rendered) + "\n"
