"""Judge declarations and scores (v2.2 D39, D73; Herd Testbed section 11.2)."""

import re
from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import Role

LATENCY_PATTERN = re.compile(r"(\d+(?:\.\d+)?)(s|m|h|d)")
SECONDS_PER_LATENCY_UNIT: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


class SourceBudget(FrozenModel):
    per_day: int = Field(ge=0)
    allocated_by: Literal["planner", "human"]


class SourceNoise(FrozenModel):
    n_for_ci: int = Field(default=1, ge=1)


class SourceDecl(FrozenModel):
    """Public description of one judge (eval/sources/<id>.yaml); seven fields are required."""

    id: str
    mode: Literal["pull", "push"]
    latency: str
    budget: SourceBudget
    cost_machine_hours: float = Field(ge=0)
    trust: float = Field(ge=0, le=1)
    readable_by: tuple[Role, ...]
    kind: Literal["tool", "dataset", "query", "flight", "human"] = "tool"
    attribution: Literal["candidate", "exposure"] = "candidate"
    concurrency: int | None = Field(default=None, ge=1)
    noise: SourceNoise = SourceNoise()
    drift: Literal["fixed", "drifting"] = "fixed"
    escalate_on_request: bool = False
    compare_against: Literal["concurrent_control", "none"] = "none"

    @model_validator(mode="after")
    def require_parseable_latency(self) -> Self:
        if LATENCY_PATTERN.fullmatch(self.latency) is None:
            raise ValueError(
                f"latency {self.latency!r} must look like 90s, 2m, 6h or 1d"
            )
        else:
            return self

    @property
    def latency_seconds(self) -> float:
        latency_match = LATENCY_PATTERN.fullmatch(self.latency)
        assert latency_match is not None
        return (
            float(latency_match.group(1))
            * SECONDS_PER_LATENCY_UNIT[latency_match.group(2)]
        )


class PerItemScore(FrozenModel):
    id: str
    value: float


class ScoreResult(FrozenModel):
    """What a judge returns for one candidate; value is None when the run was void."""

    value: float | None
    n: int = Field(ge=1)
    ci: tuple[float, float] | None = None
    per_item: tuple[PerItemScore, ...] = ()
    per_seed: tuple[float, ...] = ()
    constraint_ok: bool | None = None
    extra: dict[str, JsonValue] = {}


class Paired(FrozenModel):
    """Paired difference between a candidate and the baseline it was built on (D73).

    n_reps counts only remeasures the eval service ran itself; cache hits do not count.
    """

    mean: float
    se: float = Field(ge=0)
    n_items: int = Field(ge=0)
    n_reps: int = Field(ge=0)
