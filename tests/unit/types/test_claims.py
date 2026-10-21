"""Claim files: byte-stable rendering, parsing, and the rules a claim must meet when written."""

import pytest
from hypothesis import given
from hypothesis import strategies as strategy
from pydantic import ValidationError

from vlah.core.types.claims import Claim, EvidenceRef, parse_claim, render_claim
from vlah.core.types.ids import ActorId
from vlah.core.types.states import INITIAL_STATUS, ClaimType

CLAIM_TYPES: list[ClaimType] = ["result", "insight", "dead_end", "metric", "question"]
single_line_text = strategy.text(
    alphabet=strategy.characters(blacklist_categories=("Cs", "Cc")), min_size=1
)


@strategy.composite
def claims(draw: strategy.DrawFn) -> Claim:
    claim_type = draw(strategy.sampled_from(CLAIM_TYPES))
    return Claim(
        id=draw(single_line_text),
        type=claim_type,
        status=INITIAL_STATUS[claim_type],
        approach=draw(single_line_text),
        author=ActorId("worker-07"),
        tags=tuple(draw(strategy.lists(single_line_text, max_size=3))),
        depends_on=tuple(draw(strategy.lists(single_line_text, max_size=3))),
        evidence=(EvidenceRef(trace=draw(single_line_text)),),
        surprise=draw(strategy.booleans()),
        body=draw(
            strategy.text(alphabet=strategy.characters(blacklist_categories=("Cs",)))
        ),
    )


@given(claims())
def test_render_then_parse_returns_the_same_claim(claim: Claim) -> None:
    assert parse_claim(render_claim(claim)) == claim


def test_claim_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Claim(
            id="c-worker-07-00001",
            type="insight",
            status="proposed",
            approach="a-03",
            author=ActorId("worker-07"),
            evidence=(),
        )


def test_claim_cannot_be_written_in_a_later_status() -> None:
    """Statuses after the first come only from events (D9)."""
    with pytest.raises(ValidationError):
        Claim(
            id="c-worker-07-00001",
            type="result",
            status="verified:bench_full",
            approach="a-03",
            author=ActorId("worker-07"),
            evidence=(EvidenceRef(candidate="candidates/9f3a/result.json"),),
        )


def test_evidence_points_at_exactly_one_thing() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(trace="traces/worker-07/t-1.ptr", claim="c-worker-07-00001")
