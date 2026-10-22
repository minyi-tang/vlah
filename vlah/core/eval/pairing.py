"""Paired measurement and ladder promotion (v2.2 D73).

A candidate is compared with the baseline it was built on, seed by seed, so seed
difficulty cancels out. This is valid only when both were scored on the same
seeds in the same order, which holds for deterministic judges such as testbed
problem A; judges with model calls need the baseline run in the same batch.
"""

import math
import statistics

from vlah.core.types.sources import Paired, SourceDecl


def pair_per_seed(
    candidate_per_seed: tuple[float, ...],
    baseline_per_seed: tuple[float, ...],
    remeasure_count: int,
) -> Paired:
    """Mean and standard error of candidate minus baseline, seed by seed.

    remeasure_count is the number of runs this service submitted for the candidate;
    a baseline read from storage is a cache hit and is not counted (D73).
    """
    if not candidate_per_seed:
        raise ValueError("the judge returned no per-seed scores to pair")
    elif len(candidate_per_seed) != len(baseline_per_seed):
        raise ValueError(
            f"cannot pair {len(candidate_per_seed)} candidate seeds with "
            f"{len(baseline_per_seed)} baseline seeds"
        )
    else:
        differences = [
            candidate - baseline
            for candidate, baseline in zip(
                candidate_per_seed, baseline_per_seed, strict=True
            )
        ]
        if len(differences) >= 2:
            standard_error = statistics.stdev(differences) / math.sqrt(len(differences))
        else:
            standard_error = 0.0
        return Paired(
            mean=statistics.fmean(differences),
            se=standard_error,
            n_items=len(differences),
            n_reps=remeasure_count,
        )


def is_deterministic(declaration: SourceDecl) -> bool:
    """A judge that needs one run per confidence interval and does not drift."""
    return declaration.noise.n_for_ci == 1 and declaration.drift == "fixed"


def is_promoted(declaration: SourceDecl, paired: Paired) -> bool:
    """Whether a result earns the next, more expensive level.

    A deterministic cheap level has no standard error, so any gain counts (D73).
    A noisy level needs the gain to exceed one standard error.
    """
    if is_deterministic(declaration):
        return paired.mean > 0
    else:
        return paired.mean > paired.se
