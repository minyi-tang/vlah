"""Who may create files where on the blackboard (v2.2 section 5, D31, D63, D76).

Nobody may modify or delete an existing file; that is enforced by the blackboard.
These rules only decide who may create a path.
"""

import re
from dataclasses import dataclass

from vlah.core.types.ids import ActorId, Role, role_of_actor
from vlah.core.types.states import ALL_ROLES

OWNER_SEGMENT = "owner"


@dataclass(frozen=True, kw_only=True)
class PathRule:
    """A path pattern and the roles allowed to create files that match it.

    When the pattern has a named group "owner", that path segment must equal the
    writing actor, so each actor writes only under its own prefix.
    """

    pattern: re.Pattern[str]
    roles: frozenset[Role]


def make_path_rule(pattern: str, roles: frozenset[Role]) -> PathRule:
    return PathRule(pattern=re.compile(pattern), roles=roles)


# The first matching rule decides. events/_seq/ must come before events/<owner>/.
WRITE_RULES: tuple[PathRule, ...] = (
    make_path_rule(r"events/_seq/\d+", frozenset({"merge_queue"})),
    make_path_rule(
        r"(claims|events|traces|checkpoints)/(?P<owner>[^/]+)/.+", ALL_ROLES
    ),
    make_path_rule(
        r"candidates/[0-9a-f]+/(result|lineage)\.json", frozenset({"eval_service"})
    ),
    make_path_rule(
        r"candidates/[0-9a-f]+/(manifest\.yaml|prediction\.json|local\.json|candidate(\.[^/]+)?(/.+)?)",
        frozenset({"worker", "human"}),
    ),
    make_path_rule(r"digests/[^/]+\.md", frozenset({"librarian"})),
    make_path_rule(r"decisions/[^/]+\.md", frozenset({"planner"})),
    make_path_rule(r"allocation/[^/]+\.json", frozenset({"planner", "indexer"})),
    make_path_rule(
        r"(baseline/\d+\.json|report/.+|map/gap\.md)", frozenset({"integrator"})
    ),
    make_path_rule(r"map/measurements/[^/]+/\d+/.+", frozenset({"integrator"})),
    make_path_rule(r"map/measurements/[^/]+/[^/]+", frozenset({"worker", "human"})),
    make_path_rule(
        r"(charter/.+|spec\.md|approaches/[^/]+\.md|eval/sources/[^/]+\.yaml)",
        frozenset({"human", "pi"}),
    ),
)


def is_safe_relative_path(path: str) -> bool:
    segments = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and all(segment not in ("", ".", "..") for segment in segments)
    )


def may_create(actor: ActorId, path: str) -> bool:
    """Whether actor may create a file at path under the write rules."""
    if not is_safe_relative_path(path):
        return False
    else:
        role = role_of_actor(actor)
        for path_rule in WRITE_RULES:
            path_match = path_rule.pattern.fullmatch(path)
            if path_match is None:
                continue
            elif role not in path_rule.roles:
                return False
            elif OWNER_SEGMENT in path_rule.pattern.groupindex:
                return path_match.group(OWNER_SEGMENT) == actor
            else:
                return True
        return False
