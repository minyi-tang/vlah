"""The worker's mission, workspace and suggestions, assembled with its capabilities."""

from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.prompts.assemble import assemble
from vlah.prompts.profile import WORKER


def render_worker_prompt(agent: ActorId, task: Task) -> str:
    return assemble(WORKER, agent, task)
