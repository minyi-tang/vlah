"""FROZEN CONTRACT, owned by the Foundation (#4). Change it only in a contract-only PR.

A problem folder, loaded (v2.2 section 10, D10, D20, D45, D63). The worker-visible
part never includes herd.yaml or private/. Implemented by W1-13 (#23).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import JsonValue

from vlah.core.blackboard.api import PublicProblemFiles
from vlah.core.types.base import FrozenModel
from vlah.core.types.params import EgressPolicy, GateSpec, Limits, Params, Target
from vlah.core.types.sources import SourceDecl


class Approach(FrozenModel):
    """approaches/<id>.md: frontmatter status and parent, then the description."""

    id: str
    status: Literal["active", "frozen", "dead_end"]
    parent: str | None = None
    body: str


class AdapterChoice(FrozenModel):
    """One port's adapter in herd.yaml, for example {kind: kubernetes, namespace: herd}."""

    kind: str
    settings: dict[str, JsonValue] = {}


class JudgeConfig(FrozenModel):
    """How to run one judge: a shell command, or an adapter class "module:Class"."""

    command: str | None = None
    adapter_class: str | None = None


class HerdConfig(FrozenModel):
    """herd.yaml, which workers never see."""

    judges: dict[str, JudgeConfig]
    executor: AdapterChoice
    harness: AdapterChoice
    stores: dict[str, AdapterChoice] = {}
    models: dict[str, str] = {}
    human: AdapterChoice | None = None


@dataclass(frozen=True, kw_only=True)
class Problem:
    root: Path
    status: Literal["draft", "aligned", "revising"]
    params: Params
    gates: tuple[GateSpec, ...]
    egress: EgressPolicy
    sources: tuple[SourceDecl, ...]
    approaches: tuple[Approach, ...]
    spec_markdown: str
    limits: Limits
    target: Target | None
    public_files: PublicProblemFiles
    herd_config: HerdConfig


class LoadProblem(Protocol):
    def __call__(self, problem_root: Path) -> Problem:
        """Load and validate a problem folder; errors name the file and the field."""
        ...
