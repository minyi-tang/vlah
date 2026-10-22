"""Shared pydantic base for every cross-boundary record in the herd."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable record that rejects unknown fields and serializes by alias."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )
