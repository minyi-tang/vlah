"""Guard batches: the expensive gates, every N merges, with bisection on failure (D74).

Merged baselines are guard_pending until their batch passes; workers may build on
them. When a batch fails, the culprit merge is found by bisection over the versions
since the last guarded one, the core reverts it (a new version; records are never
changed), and the new head is checked again. check is one step of that: the
service calls it whenever a batch is due and no measurement is pending, so a
revert's measurement never blocks the supervisor's tick. The deliverable is never a
guard_pending version.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vlah.core.integrator.core_adapter import CoreAdapter, Version, find_culprit
from vlah.core.types.params import GateSpec, GateStage

logger = logging.getLogger(__name__)

GUARD: GateStage = "guard"
# A batch holds guard_every_merges merges; the bound covers a batch where every
# merge fails on its own. Past it the batch is abandoned and the last guarded
# version stays the deliverable.
MAX_REVERTS_PER_BATCH = 8


class GateVerdict(Protocol):
    @property
    def passed(self) -> bool: ...


class GuardGateRunner(Protocol):
    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateVerdict:
        """Run one guard gate against a baseline version's files."""
        ...


@dataclass(kw_only=True)
class GuardBatches:
    gates: tuple[GateSpec, ...]
    gate_runner: GuardGateRunner
    every_merges: int
    last_guarded: int = 0
    merges_since: int = 0
    reverts_in_batch: int = 0
    is_bisected: bool = False
    pending: set[int] = field(default_factory=set[int])
    outcomes: dict[int, dict[str, bool]] = field(
        default_factory=dict[int, dict[str, bool]]
    )

    @property
    def guard_gates(self) -> tuple[GateSpec, ...]:
        return tuple(gate for gate in self.gates if GUARD in gate.when)

    @property
    def enabled(self) -> bool:
        return bool(self.guard_gates)

    @property
    def is_abandoned(self) -> bool:
        return self.reverts_in_batch >= MAX_REVERTS_PER_BATCH

    def is_pending(self, k: int) -> bool:
        return k in self.pending

    def mark(self, version: Version) -> None:
        """A merged version is guard_pending until its batch passes; reverts are not merges."""
        if self.enabled and version.added is not None:
            self.pending.add(version.k)
            self.merges_since += 1
        else:
            pass

    def is_due(self) -> bool:
        """A batch is due every every_merges merges, until it settles or is abandoned."""
        return (
            self.enabled
            and not self.is_abandoned
            and self.merges_since >= self.every_merges
        )

    def is_unguarded(self, core: CoreAdapter) -> bool:
        """At closeout every merge since the last guarded version is checked (D79)."""
        return self.enabled and not self.is_abandoned and core.k != self.last_guarded

    def run_gates(self, version: Version) -> dict[str, bool]:
        """Every guard gate on a version's files; remembered per version."""
        if version.k in self.outcomes:
            return self.outcomes[version.k]
        else:
            results = {
                gate.name: self.gate_runner(gate, Path(version.dir)).passed
                for gate in self.guard_gates
            }
            self.outcomes[version.k] = results
            return results

    def passes(self, version: Version) -> bool:
        return all(self.run_gates(version).values())

    def check(self, core: CoreAdapter) -> Version | None:
        """One step of a batch: settle the head if its gates pass, else revert one merge.

        The first failure is bisected over the versions since the last guarded one.
        After a revert those versions are no longer one chain (the ones between
        still hold the culprit), so if the head fails again the remaining merges of
        the batch are reverted one at a time, newest first. Returns the version the
        core wrote, if any.
        """
        head = core.current()
        if self.passes(head):
            self.settle(head.k)
            return None
        elif not self.is_bisected:
            self.is_bisected = True
            culprit = find_culprit(
                self.last_guarded,
                head.k,
                lambda low, high: not self.passes(core.versions[high]),
            )
            logger.warning(
                f"guard batch failed at baseline@{head.k}; reverting the merge that "
                f"made baseline@{culprit}"
            )
            return self.revert(core, culprit)
        else:
            suspect = self.newest_suspect(core, head)
            if suspect is None:
                self.reverts_in_batch = MAX_REVERTS_PER_BATCH
                logger.error(
                    f"baseline@{head.k} fails its guard gates with every merge of the "
                    f"batch reverted; baseline@{self.last_guarded} stays the last "
                    "guarded version"
                )
                return None
            else:
                logger.warning(
                    f"baseline@{head.k} still fails its guard gates; reverting the "
                    f"merge that made baseline@{suspect}"
                )
                return self.revert(core, suspect)

    def revert(self, core: CoreAdapter, k: int) -> Version:
        self.reverts_in_batch += 1
        return core.revert(k - 1, k, GUARD)

    def newest_suspect(self, core: CoreAdapter, head: Version) -> int | None:
        """The newest merge of this batch whose candidate the head still holds."""
        for k in range(head.k, self.last_guarded, -1):
            version = core.versions[k]
            if version.added is not None and version.added in head.candidates:
                return k
            else:
                continue
        return None

    def settle(self, k: int) -> None:
        """Everything up to k passed: nothing before it is pending any more."""
        self.last_guarded = k
        self.merges_since = 0
        self.reverts_in_batch = 0
        self.is_bisected = False
        self.pending = {pending for pending in self.pending if pending > k}

    def deliverable(self, core: CoreAdapter) -> Version:
        """The head once its batch passed, else the last version whose batch did."""
        head = core.current()
        if not self.enabled or head.k == self.last_guarded:
            return head
        else:
            return core.versions[self.last_guarded]
