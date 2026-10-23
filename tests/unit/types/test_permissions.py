"""Actor roles and blackboard write rules (v2.2 section 5, D31, D63)."""

import pytest

from vlah.core.types.ids import ActorId, Role, role_of_actor
from vlah.core.types.paths import may_create


@pytest.mark.parametrize(
    ("actor", "role"),
    [
        ("worker-07", "worker"),
        ("critic-1", "critic"),
        ("eval_service", "eval_service"),
        ("queue", "indexer"),
        ("merge_queue", "merge_queue"),
    ],
)
def test_role_is_read_from_the_whole_actor_id(actor: str, role: Role) -> None:
    assert role_of_actor(ActorId(actor)) == role


@pytest.mark.parametrize(
    "actor", ["eval_service-x", "worker-", "worker-07x", "Worker-1", ""]
)
def test_unknown_actor_ids_are_rejected(actor: str) -> None:
    """A loose prefix match would let 'eval_service-x' write result.json."""
    with pytest.raises(ValueError, match="unknown actor id"):
        role_of_actor(ActorId(actor))


@pytest.mark.parametrize(
    ("actor", "path", "is_allowed"),
    [
        ("worker-07", "claims/worker-07/c-worker-07-00001.md", True),
        ("worker-07", "claims/worker-08/c-worker-08-00001.md", False),
        ("worker-07", "claims/worker-07/../worker-08/c.md", False),
        ("worker-07", "candidates/9f3a/manifest.yaml", True),
        ("worker-07", "candidates/9f3a/candidate/policy.py", True),
        ("worker-07", "candidates/9f3a/result.json", False),
        ("eval_service", "candidates/9f3a/result.json", True),
        ("integrator", "candidates/77be/candidate/policy.py", True),
        ("eval_service", "candidates/77be/manifest.yaml", False),
        ("worker-07", "events/_seq/10482", False),
        ("merge_queue", "events/_seq/10482", True),
        ("worker-07", "map/measurements/wait_by_site/measure.py", True),
        ("worker-07", "map/measurements/wait_by_site/7/output.json", False),
        ("integrator", "map/measurements/wait_by_site/7/output.json", True),
        ("integrator", "baseline/7.json", True),
        ("worker-07", "charter/constraints.md", False),
        ("human", "charter/constraints.md", True),
        ("worker-07", "private/judge.py", False),
    ],
)
def test_write_rules(actor: str, path: str, is_allowed: bool) -> None:
    assert may_create(ActorId(actor), path) is is_allowed


def test_the_human_seeds_the_opportunity_map_and_the_integrator_updates_it() -> None:
    """D76: a human writes the first map when setting up the problem."""
    assert may_create(ActorId("human"), "map/gap.md")
    assert may_create(ActorId("integrator"), "map/gap.md")
    assert may_create(ActorId("pi"), "map/gap.md")
    assert not may_create(ActorId("worker-01"), "map/gap.md")
    assert not may_create(ActorId("human"), "baseline/1.json")
