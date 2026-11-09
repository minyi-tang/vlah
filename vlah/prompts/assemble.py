"""The one prompt assembly every role shares (D35, D1; docs/role-prompts.md).

The sections come in a fixed order so that a change to a later part leaves the
earlier text unchanged: mission, workspace, boundaries (the invariants plus one
line per capability), one card per capability, suggestions, then the task. The
prompt states a goal and never tells the agent what to read; CONTEXT.md carries
the material and the notices the herd appends.
"""

from importlib.resources import files
from string import Template

from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.prompts.profile import RoleProfile

BOUNDARIES = files("vlah.prompts").joinpath("boundaries.md")
# Words per part, checked by a test: discipline, not a token count.
MISSION_WORDS = 150
WORKSPACE_WORDS = 80
BOUNDARIES_WORDS = 250
CARD_WORDS = 250
SUGGESTIONS_WORDS = 120


def role_text(profile: RoleProfile, name: str) -> str:
    return files(profile.prompt_package).joinpath(f"{name}.md").read_text("utf-8")


def task_section(task: Task) -> str:
    lines = [f"Your goal: {task.goal}"]
    if task.refs:
        lines.append(f"It is about: {', '.join(task.refs)}.")
    else:
        pass
    return "\n\n".join(lines)


def assemble(profile: RoleProfile, agent: ActorId, task: Task) -> str:
    boundaries = BOUNDARIES.read_text("utf-8").rstrip() + "".join(
        f"\n- {capability.boundary}" for capability in profile.capabilities
    )
    sections = [
        ("Mission", role_text(profile, "mission")),
        ("Workspace", role_text(profile, "workspace")),
        ("Boundaries", boundaries),
        *(
            (f"Tools: {capability.name}", capability.card)
            for capability in profile.capabilities
        ),
        ("Suggestions", role_text(profile, "suggestions")),
        ("Your task", task_section(task)),
    ]
    rendered = [
        f"# {heading}\n\n" + Template(text).substitute(agent=agent).strip()
        for heading, text in sections
    ]
    return "\n\n".join(rendered) + "\n"
