"""Claims: one contribution per blackboard file, YAML frontmatter plus a body (v2.2 section 5)."""

from typing import Self

import yaml
from pydantic import Field, model_validator

from vlah.core.types.base import FrozenModel
from vlah.core.types.ids import ActorId
from vlah.core.types.states import INITIAL_STATUS, ClaimType

FRONTMATTER_FENCE = "---\n"


class EvidenceRef(FrozenModel):
    """One pointer that backs a claim; exactly one field is set."""

    trace: str | None = None
    candidate: str | None = None
    claim: str | None = None
    path: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_pointer(self) -> Self:
        pointers = [self.trace, self.candidate, self.claim, self.path]
        pointer_count = sum(1 for pointer in pointers if pointer is not None)
        if pointer_count != 1:
            raise ValueError(f"evidence needs exactly one pointer, got {pointer_count}")
        else:
            return self


class Claim(FrozenModel):
    """A claim as written; its current status is derived from events.

    reuse_count is derived by the indexer and never stored, because files are append-only.
    """

    id: str
    type: ClaimType
    status: str
    approach: str
    author: ActorId
    tags: tuple[str, ...] = ()
    blamed_constraint: str | None = None
    depends_on: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1)
    surprise: bool = False
    body: str = ""

    @model_validator(mode="after")
    def require_initial_status(self) -> Self:
        expected_status = INITIAL_STATUS[self.type]
        if self.status != expected_status:
            raise ValueError(
                f"a {self.type} claim is written with status {expected_status!r}, "
                f"not {self.status!r}; later statuses come from events"
            )
        else:
            return self


def render_claim(claim: Claim) -> str:
    """Serialize a claim to its file text; the output is byte-stable."""
    frontmatter = claim.model_dump(mode="json", exclude={"body"}, exclude_none=True)
    frontmatter_text = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return f"{FRONTMATTER_FENCE}{frontmatter_text}{FRONTMATTER_FENCE}{claim.body}"


def parse_claim(text: str) -> Claim:
    """Parse a claim file written by render_claim or by hand."""
    if not text.startswith(FRONTMATTER_FENCE):
        raise ValueError("a claim file must start with a '---' frontmatter line")
    else:
        closing_index = text.find(f"\n{FRONTMATTER_FENCE}", len(FRONTMATTER_FENCE) - 1)
        if closing_index < 0:
            raise ValueError("the claim frontmatter has no closing '---' line")
        else:
            frontmatter_text = text[len(FRONTMATTER_FENCE) : closing_index + 1]
            body = text[closing_index + 1 + len(FRONTMATTER_FENCE) :]
            frontmatter: object = yaml.safe_load(frontmatter_text)
            claim = Claim.model_validate(frontmatter)
            return claim.model_copy(update={"body": body})
