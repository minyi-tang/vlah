"""The shared merger as the herd sees it (TB section 11.8.1, D74).

The merge rules live in the testbed: testbed.integrator.IntegratorCore is the one
testbed module the herd imports. This module names what the wrapper uses of it as a
Protocol, maps the herd's Params onto its configuration, and builds it from the
calibration the runtime supplies. The core takes an injected clock and an injected
Measurer, so the wrapper decides when it runs and through what it spends.
"""

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from testbed.integrator.core import (
    IntegratorConfig,
    IntegratorCore,
    Measurement,
    Measurer,
    QuotaExhausted,
    VerifiedCandidate,
    Version,
)
from vlah.core.types.clock import Clock
from vlah.core.types.params import Params

# A core decision is an event-shaped dict from untyped testbed code: a boundary,
# narrowed wherever a field is read.
Decision = dict[str, object]


@dataclass(frozen=True, kw_only=True)
class CoreCalibration:
    """What the core knows about baseline@0 before any merge (D72).

    per_seed and score are baseline@0's top-level daily scores; audit_per_seed its
    audit-set scores, or None when the core should measure them at the first spot
    check; sigma_dev and sigma_audit are the measurement noise of the two sets. The
    runtime reads them from the judge's calibration; tests make them up.
    """

    per_seed: tuple[float, ...]
    score: float
    audit_per_seed: tuple[float, ...] | None
    sigma_dev: float
    sigma_audit: float

    def as_core_dict(self) -> dict[str, object]:
        return {
            "baseline_slow": {"per_seed": list(self.per_seed), "score": self.score},
            "baseline_audit": (
                None
                if self.audit_per_seed is None
                else {"per_seed": list(self.audit_per_seed)}
            ),
            "audit_sigma": {"dev": self.sigma_dev, "audit": self.sigma_audit},
        }


class SegmentCheck(Protocol):
    def __call__(self, low: int, high: int) -> bool:
        """True when the merges in (low, high] include the one that broke the gates."""
        ...


class Waiter(Protocol):
    def __call__(self) -> None:
        """Let a pending measurement finish: tick the eval client, let time pass."""
        ...


class AdvanceListener(Protocol):
    def __call__(self, version: Version) -> None:
        """A version is complete: merged, reverted, or measured after a revert."""
        ...


class CoreAdapter(Protocol):
    """What the wrapper uses of the core; the core satisfies it as it is."""

    versions: list[Version]
    queue: deque[VerifiedCandidate]
    closed: bool

    @property
    def k(self) -> int: ...

    def current(self) -> Version: ...

    def add(self, candidate: VerifiedCandidate) -> None: ...

    def due(self, now: float) -> str | float | None:
        """'now', 'wait' while a measurement is pending, a time, or None."""
        ...

    def step(self) -> list[Decision]: ...

    def revert(self, lo: int, hi: int, reason: str) -> Version: ...

    def closeout(
        self, wait: Waiter | None = None
    ) -> tuple[Version, list[Decision]]: ...


def config_from_params(params: Params) -> IntegratorConfig:
    """The core's settings from the herd's Params; equal in every arm (L0-19).

    accept_rule has no counterpart in the core, whose only acceptance rule is the
    paired gain against accept_se standard errors.
    """
    return IntegratorConfig(
        accept_se=params.accept_min_standard_errors,
        interaction_k=params.interaction_sigma_multiple,
        audit_every=params.spot_check_every_merges,
        audit_k=params.spot_check_sigma_multiple,
        audit_max=params.holdout_run_cap,
        audit_reserve=params.spot_check_reserved_for_bisection,
    )


def build_core(
    *,
    params: Params,
    work_directory: Path,
    baseline_directory: Path,
    calibration: CoreCalibration,
    clock: Clock,
    measurer: Measurer,
    on_advance: AdvanceListener,
) -> IntegratorCore:
    return IntegratorCore(
        config_from_params(params),
        work_directory,
        baseline_directory,
        calibration.as_core_dict(),
        clock.now_seconds,
        measurer,
        on_advance,
    )


def find_culprit(low: int, high: int, segment_is_bad: SegmentCheck) -> int:
    """The first merge in (low, high] after which the check fails: the core's bisection."""
    return IntegratorCore.find_culprit(low, high, segment_is_bad)


__all__ = [
    "AdvanceListener",
    "CoreAdapter",
    "CoreCalibration",
    "Decision",
    "IntegratorConfig",
    "Measurement",
    "Measurer",
    "QuotaExhausted",
    "SegmentCheck",
    "VerifiedCandidate",
    "Version",
    "Waiter",
    "build_core",
    "config_from_params",
    "find_culprit",
]
