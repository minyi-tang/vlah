"""Which candidates a claim is about, for dead ends and the testbed recorder (TB§11.4, §11.5).

A claim names a candidate directly (candidates/<hash>/...) or through another claim,
such as the eval service's result claim, whose own evidence names the candidate.
"""

import logging
import re

from pydantic import JsonValue

from vlah.core.blackboard.api import Blackboard
from vlah.core.types.claims import Claim, parse_claim
from vlah.tools.scope import CANDIDATE_POINTER_PATTERN

logger = logging.getLogger(__name__)

CLAIM_ID_PATTERN = re.compile(r"c-(?P<author>.+)-\d+")
# A claim that records composing two candidates carries this tag (TB section 11.5,
# conflict handling).
COMPOSE_TAG = "compose"
RECORDER_KIND_BY_CLAIM_TYPE = {"dead_end": "dead_end", "insight": "insight"}


def direct_candidate_hashes(claim: Claim) -> list[str]:
    pointers = [
        pointer
        for evidence in claim.evidence
        for pointer in (evidence.candidate, evidence.path, evidence.trace)
        if pointer is not None
    ]
    return [
        match.group("hash")
        for pointer in pointers
        if (match := CANDIDATE_POINTER_PATTERN.match(pointer)) is not None
    ]


def cited_claim(blackboard: Blackboard, claim_id: str) -> Claim | None:
    match = CLAIM_ID_PATTERN.fullmatch(claim_id)
    if match is None:
        return None
    else:
        path = f"claims/{match.group('author')}/{claim_id}.md"
        if path not in blackboard.list_paths(path, None):
            return None
        else:
            try:
                return parse_claim(blackboard.read(path, None).decode("utf-8"))
            except ValueError:
                logger.warning(f"{path} does not parse as a claim")
                return None


def candidate_hashes(blackboard: Blackboard, claim: Claim) -> tuple[str, ...]:
    """The candidates a claim names directly, then through the claims it cites."""
    hashes = direct_candidate_hashes(claim)
    for evidence in claim.evidence:
        cited = (
            None if evidence.claim is None else cited_claim(blackboard, evidence.claim)
        )
        if cited is not None:
            hashes.extend(direct_candidate_hashes(cited))
        else:
            pass
    return tuple(dict.fromkeys(hashes))


def write_event_data(claim: Claim, hashes: tuple[str, ...]) -> dict[str, JsonValue]:
    """The blackboard_write recorder event for a merged claim."""
    if COMPOSE_TAG in claim.tags:
        kind = "compose"
    else:
        kind = RECORDER_KIND_BY_CLAIM_TYPE.get(claim.type, "claim")
    data: dict[str, JsonValue] = {"kind": kind, "id": claim.id, "refs": list(hashes)}
    if claim.type == "dead_end":
        data["hash"] = hashes[0]
    else:
        pass
    return data


def parse_claims(files: dict[str, str]) -> tuple[list[Claim], list[str]]:
    """Claims among the files, and an error for each one that does not parse."""
    claims: list[Claim] = []
    errors: list[str] = []
    for path in sorted(files):
        if path.startswith("claims/") and path.endswith(".md"):
            try:
                claims.append(parse_claim(files[path]))
            except ValueError as error:
                errors.append(f"{path}: not a valid claim: {error}")
        else:
            continue
    return claims, errors
