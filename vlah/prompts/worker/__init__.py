"""The worker prompt: the goal and taste, the enforced rules, the suggestions (D1, D35).

The three parts are separate files because the P11 decision may move items between
the rules and the suggestions. The prompt states a goal and never tells the agent to
read particular material (D1); CONTEXT.md carries the material.
"""

from importlib.resources import files
from string import Template

from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task

PARTS = (
    ("Goal", "goal.md"),
    ("Rules the herd enforces", "rules.md"),
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
