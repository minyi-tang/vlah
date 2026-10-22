"""Actor identities, roles, write priority and id formats (v2.2 D34, D56)."""

import re
from typing import Literal, NewType

ActorId = NewType("ActorId", str)

Role = Literal[
    "human",
    "critic",
    "eval_service",
    "ci",
    "indexer",
    "planner",
    "scout",
    "librarian",
    "integrator",
    "worker",
    "merge_queue",
    "pi",
]

# Lower number wins a conflict between events (D34). merge_queue and pi are not
# in D34; they sit with the system writers and the planner respectively.
WRITE_PRIORITY: dict[Role, int] = {
    "human": 0,
    "critic": 1,
    "eval_service": 2,
    "ci": 2,
    "integrator": 2,
    "merge_queue": 2,
    "planner": 3,
    "pi": 3,
    "indexer": 4,
    "scout": 5,
    "librarian": 5,
    "worker": 5,
}

NUMBERED_ACTOR_ROLES: dict[str, Role] = {
    "worker": "worker",
    "librarian": "librarian",
    "critic": "critic",
    "scout": "scout",
    "planner": "planner",
}

NUMBERED_ACTOR_PATTERN = re.compile(rf"({'|'.join(NUMBERED_ACTOR_ROLES)})-(\d+)")

SINGLETON_ACTOR_ROLES: dict[str, Role] = {
    "human": "human",
    "eval_service": "eval_service",
    "ci": "ci",
    "indexer": "indexer",
    "queue": "indexer",
    "integrator": "integrator",
    "merge_queue": "merge_queue",
    "pi": "pi",
    "scout": "scout",
    "planner": "planner",
}


def role_of_actor(actor: ActorId) -> Role:
    """Return the role encoded in an actor id; unknown ids are an error.

    Blackboard write permissions depend on this, so it matches whole ids only.
    """
    numbered_match = NUMBERED_ACTOR_PATTERN.fullmatch(actor)
    if numbered_match is not None:
        return NUMBERED_ACTOR_ROLES[numbered_match.group(1)]
    elif actor in SINGLETON_ACTOR_ROLES:
        return SINGLETON_ACTOR_ROLES[actor]
    else:
        raise ValueError(f"unknown actor id {actor!r}")


def format_claim_id(author: ActorId, number: int) -> str:
    """Claim id that is unique without coordination between writers."""
    return f"c-{author}-{number:05d}"


def format_event_local_id(actor: ActorId, number: int) -> str:
    """Writer-local event id; it is also the event file name (D56)."""
    return f"{actor}/{number:05d}"


def format_task_id(number: int) -> str:
    return f"t-{number:05d}"
