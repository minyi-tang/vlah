"""baseline/<k>.json: one record per baseline version, never rewritten (v2.2 D74)."""

from pydantic import Field

from vlah.core.types.base import FrozenModel
from vlah.core.types.sources import Paired


class BaselineRecord(FrozenModel):
    """A baseline version; a revert writes a new k that excludes the culprit."""

    k: int = Field(ge=0)
    parent: int | None
    candidates: tuple[str, ...]
    added: str | None = None
    removed: tuple[str, ...] = ()
    paired: Paired | None = None
    per_seed: tuple[float, ...] = ()
    gates: dict[str, bool] = {}
    guard_pending: bool = False
    created_ts: float
