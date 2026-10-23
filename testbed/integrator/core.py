"""Integrator core (TB 11.8.1; design doc D72, D74, D79), as a library.

Shared by all three arms, and wrapped by the herd's Integrator (issue #10, 3d). It never talks
HTTP: measurements go through an injected Measurer, time comes from an injected clock, and every
decision is returned as data from step(). testbed.integrator.driver wires it to the slow-eval
service for the single and isolated arms.

    core = IntegratorCore(config, workdir, baseline_dir, calibration, clock, measurer)
    core.add(VerifiedCandidate(...))      # first in, first out
    decisions = core.step()               # baseline_advanced, integration_failed, ...
    core.closeout(wait)                   # D79: stop accepting, final audit, freeze
"""

from __future__ import annotations

import json
import math
import re
import shutil
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from testbed.integrator.merge import Conflict, copy_tree, distance, three_way
from testbed.problem_api import content_hash, mean, se

AUDIT_GAP_TOLERANCE = 1e-9  # float slack when a gap is compared with its threshold


@dataclass(frozen=True)
class IntegratorConfig:
    """Identical in every arm (11.8.1). Equality is the cross-arm check (L0-19)."""

    accept_se: float = 1.0  # accept when paired gain > accept_se * standard error
    interaction_k: float = (
        2.0  # earlier - remeasured > k * sqrt(se1^2 + se2^2), i.e. 2 sigma sqrt2
    )
    audit_every: int = 2  # audit after every N merges
    audit_k: float = (
        2.5  # flag when |dev diff - audit diff| > k * sqrt(sd_dev^2 + sd_audit^2)
    )
    audit_max: int = 10  # audit-set measurements per run
    audit_reserve: int = 3  # kept for bisection and the final audit
    poll_s: float = 5.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "IntegratorConfig":
        return cls(**json.loads(text))


@dataclass
class VerifiedCandidate:
    """A candidate whose slow-tier result is done and constraint_ok (what the core accepts)."""

    ticket: str
    hash: str
    target: str  # "baseline@k" it claims to be built on
    per_seed: list
    score: float
    files: Path  # its payload directory
    actor: str | None = None


@dataclass
class Measurement:
    ticket: str
    hash: str | None
    per_seed: list
    score: float | None
    constraint_ok: bool


class QuotaExhausted(Exception):
    """Raised by Measurer.submit. next_slot_at is None when the run's total is used up."""

    def __init__(self, next_slot_at: float | None):
        super().__init__(next_slot_at)
        self.next_slot_at = next_slot_at


class Measurer(Protocol):
    """How the core gets paired re-measures and audit measurements (the herd passes its EvalService)."""

    def submit(
        self,
        candidate_dir: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents: list[str],
        exempt: bool,
        predicted: float,
    ) -> str:
        """Queue a measurement of candidate_dir on baseline@<baseline>; return its ticket."""
        ...

    def result(self, ticket: str) -> Measurement | None:
        """The finished measurement, or None while it is pending."""
        ...


@dataclass
class Version:
    """One baseline version. record() is the herd's BaselineRecord; the rest is testbed bookkeeping."""

    k: int
    dir: str
    per_seed: list
    score: float | None
    ci: list | None = None
    ticket: str | None = None
    hash: str | None = None
    candidates: list = field(default_factory=list)
    parent: int | None = None
    added: str | None = None
    removed: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    audit_per_seed: list | None = None
    constraint_ok: bool = True
    paired: dict | None = None
    created_ts: float = 0.0
    note: str = ""

    def record(self) -> dict:
        """Exactly the fields of vlah.core.types.baseline.BaselineRecord."""
        return {
            "k": self.k,
            "parent": self.parent,
            "candidates": list(self.candidates),
            "added": self.added,
            "removed": list(self.removed),
            "paired": self.paired,
            "per_seed": list(self.per_seed),
            "gates": {},
            "guard_pending": False,
            "created_ts": self.created_ts,
        }


def paired(candidate_per_seed, baseline_per_seed) -> dict:
    """Per-seed paired difference: {mean, se, n_items, n_reps} as in the herd's Paired."""
    differences = [
        candidate - baseline
        for candidate, baseline in zip(candidate_per_seed, baseline_per_seed)
    ]
    return {
        "mean": mean(differences) if differences else 0.0,
        "se": se(differences),
        "n_items": len(differences),
        "n_reps": 0,
    }


def target_index(target: str | None) -> int | None:
    match = re.fullmatch(r"baseline@(\d+)", str(target or "baseline@0"))
    return int(match.group(1)) if match else None


class IntegratorCore:
    def __init__(
        self,
        config: IntegratorConfig,
        workdir: Path,
        baseline_dir: Path,
        calibration: dict,
        clock: Callable[[], float],
        measurer: Measurer,
        on_advance: Callable[[Version], None] | None = None,
    ):
        """calibration: baseline_slow and baseline_audit aggregates (per_seed, score, ci) and audit_sigma {dev, audit}."""
        self.config = config
        self.workdir = Path(workdir)
        (self.workdir / "baseline").mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.measurer = measurer
        self.on_advance = on_advance
        self.audit_sigma = calibration.get("audit_sigma") or {"dev": 0.0, "audit": 0.0}
        version0_dir = self.workdir / "baseline" / "0"
        copy_tree(baseline_dir, version0_dir)
        baseline_slow = calibration["baseline_slow"]
        self.versions = [
            Version(
                0,
                str(version0_dir),
                baseline_slow["per_seed"],
                baseline_slow["score"],
                baseline_slow.get("ci"),
                audit_per_seed=(calibration.get("baseline_audit") or {}).get(
                    "per_seed"
                ),
                created_ts=clock(),
                note="baseline@0",
            )
        ]
        self.write_version(self.versions[0])
        self.queue: deque = deque()
        self.pending_decisions: list[dict] = []
        self.task: dict | None = None
        self.wait_until = 0.0
        self.quota_exhausted = False
        self.merges_since_audit = 0
        self.last_audited_k = 0
        self.audits_used = 0
        self.closed = False
        self.frozen: Version | None = None
        self.compose_task_count = 0
        self.quarantined: set[str] = set()  # content hashes removed by an audit
        self.content_hash_by_hash: dict[str, str] = {}
        self.stats = {
            "remeasures": 0,
            "audits": 0,
            "compose_tasks": 0,
            "merged": 0,
            "failed": 0,
        }

    @property
    def k(self) -> int:
        return len(self.versions) - 1

    def current(self) -> Version:
        return self.versions[-1]

    def emit(self, event_type: str, data: dict):
        self.pending_decisions.append(dict(data, type=event_type))

    def write_version(self, version: Version):
        (self.workdir / "baseline" / f"{version.k}.json").write_text(
            json.dumps(version.record(), indent=1, default=str)
        )
        (self.workdir / "baseline" / f"{version.k}.testbed.json").write_text(
            json.dumps(asdict(version), indent=1, default=str)
        )

    def add(self, candidate: VerifiedCandidate):
        """Queue a verified candidate (first in, first out by verification time)."""
        if not self.closed:
            self.queue.append(candidate)

    def due(self, now: float):
        """When step() next has work: 'now', 'wait' (a measurement is pending), a time, or None."""
        if self.task is not None:
            return "wait"
        if self.closed:
            return None
        if (
            self.merges_since_audit >= self.config.audit_every
            and self.audits_left(periodic=True) > 0
        ):
            return "now"
        if not self.queue:
            return None
        if any(target_index(candidate.target) == self.k for candidate in self.queue):
            return "now"
        if self.quota_exhausted:
            return None
        return "now" if now >= self.wait_until else self.wait_until

    def step(self) -> list[dict]:
        """Advance by at most one action; return the decisions made (as event-shaped dicts)."""
        if self.task is not None:
            measurement = self.measurer.result(self.task["ticket"])
            if measurement is not None:
                task, self.task = self.task, None
                getattr(self, "on_" + task["kind"])(task, measurement)
        elif not self.closed:
            if (
                self.merges_since_audit >= self.config.audit_every
                and self.audits_left(periodic=True) > 0
            ):
                self.start_audit()
            else:
                self.process_next()
        decisions, self.pending_decisions = self.pending_decisions, []
        return decisions

    def audits_left(self, periodic: bool) -> int:
        left = self.config.audit_max - self.audits_used
        return left - self.config.audit_reserve if periodic else left

    def submit(
        self,
        candidate_dir: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents=(),
        exempt=False,
        predicted: float | None = None,
    ) -> str:
        return self.measurer.submit(
            candidate_dir,
            baseline,
            dataset,
            note,
            list(parents),
            exempt,
            (
                predicted
                if predicted is not None
                else (self.versions[baseline].score or 0.0)
            ),
        )

    def process_next(self):
        blocked = self.clock() < self.wait_until or self.quota_exhausted
        for index, candidate in enumerate(self.queue):
            if blocked and target_index(candidate.target) != self.k:
                continue
            del self.queue[index]
            self.integrate(candidate)
            return

    def integrate(self, candidate: VerifiedCandidate):
        target_k = target_index(candidate.target)
        if target_k is None or target_k > self.k:
            self.fail(candidate, "invalid_target")
            return
        payload_dir = Path(candidate.files)
        self.content_hash_by_hash[candidate.hash] = content_hash(payload_dir)
        if self.content_hash_by_hash[candidate.hash] in self.quarantined:
            self.fail(candidate, "quarantined")
            return
        claimed_k = target_k
        target_k = self.effective_base(payload_dir, target_k)
        if target_k != claimed_k:
            self.emit(
                "stale_target",
                {
                    "candidate": candidate.hash,
                    "claimed": claimed_k,
                    "effective": target_k,
                    "actor": candidate.actor,
                },
            )
        rebased_dir = self.workdir / "rebased" / f"{candidate.hash[:16]}@{self.k}"
        try:
            changed_files = three_way(
                Path(self.versions[target_k].dir),
                Path(self.versions[self.k].dir),
                payload_dir,
                rebased_dir,
            )
        except Conflict as conflict:
            self.compose(candidate, target_k, payload_dir, str(conflict))
            return
        earlier_paired = paired(candidate.per_seed, self.versions[target_k].per_seed)
        if target_k == self.k:
            self.decide(
                candidate,
                rebased_dir,
                changed_files,
                earlier_paired,
                Measurement(
                    candidate.ticket,
                    candidate.hash,
                    candidate.per_seed,
                    candidate.score,
                    True,
                ),
            )
            return
        if not changed_files:
            self.fail(
                candidate,
                "below_1se",
                paired_gain={"mean": 0.0, "se": 0.0, "n_items": 0, "n_reps": 0},
                note="no change after rebase",
            )
            return
        try:
            ticket = self.submit(
                rebased_dir,
                self.k,
                "dev",
                f"remeasure {candidate.hash[:12]} on baseline@{self.k}",
                parents=[candidate.hash],
                predicted=candidate.score,  # its own score; earlier is relative to v_t, not v_k
            )
        except QuotaExhausted as exhausted:
            self.queue.appendleft(candidate)
            if exhausted.next_slot_at:
                self.wait_until = exhausted.next_slot_at
            else:
                self.quota_exhausted = True
            return
        self.stats["remeasures"] += 1
        self.task = {
            "kind": "remeasure",
            "ticket": ticket,
            "candidate": candidate,
            "rebased_dir": str(rebased_dir),
            "changed_files": changed_files,
            "earlier_paired": earlier_paired,
            "base_k": self.k,
        }

    def effective_base(self, payload_dir: Path, claimed_k: int) -> int:
        """The version a candidate was really built on: the one nearest to it by content, whatever its label
        says (agents keep editing work/ after a merge, and copy old directories). Ties go to the claimed
        version, then to the older one: an older base can only cause a conflict (a compose task), while a
        newer base the candidate lacks would silently drop that version's changes."""
        distances = [
            distance(payload_dir, Path(version.dir)) for version in self.versions
        ]
        nearest = min(distances)
        tied_versions = [
            index
            for index, version_distance in enumerate(distances)
            if version_distance == nearest
        ]
        return claimed_k if claimed_k in tied_versions else tied_versions[0]

    def on_remeasure(self, task, measurement: Measurement):
        candidate = task["candidate"]
        if (
            task["base_k"] != self.k
        ):  # the baseline moved under us (revert): try again later
            self.queue.appendleft(candidate)
            return
        measurement.constraint_ok = (
            measurement.constraint_ok and measurement.score is not None
        )
        self.decide(
            candidate,
            Path(task["rebased_dir"]),
            task["changed_files"],
            task["earlier_paired"],
            measurement,
            remeasured=True,
        )

    def decide(
        self,
        candidate: VerifiedCandidate,
        rebased_dir: Path,
        changed_files,
        earlier_paired,
        measurement: Measurement,
        remeasured=False,
    ):
        current_version = self.versions[self.k]
        paired_gain = (
            paired(measurement.per_seed, current_version.per_seed)
            if measurement.score is not None
            else None
        )
        if paired_gain is not None and remeasured:
            paired_gain["n_reps"] = 1
            interaction_threshold = self.config.interaction_k * math.sqrt(
                earlier_paired["se"] ** 2 + paired_gain["se"] ** 2
            )
            if earlier_paired["mean"] - paired_gain["mean"] > interaction_threshold:
                self.emit(
                    "interaction_found",
                    {
                        "candidate": candidate.hash,
                        "baseline": self.k,
                        "earlier": earlier_paired,
                        "remeasured": paired_gain,
                        "threshold": interaction_threshold,
                        "overlaps_with": list(current_version.candidates),
                    },
                )
        if not measurement.constraint_ok or paired_gain is None:
            self.fail(candidate, "constraint", paired_gain=paired_gain)
            return
        if not (paired_gain["mean"] > self.config.accept_se * paired_gain["se"]):
            self.fail(candidate, "below_1se", paired_gain=paired_gain)
            return
        next_k = self.k + 1
        version_dir = self.workdir / "baseline" / str(next_k)
        copy_tree(rebased_dir, version_dir)
        version = Version(
            next_k,
            str(version_dir),
            measurement.per_seed,
            measurement.score,
            ticket=measurement.ticket,
            hash=measurement.hash,
            candidates=current_version.candidates + [candidate.hash],
            parent=self.k,
            added=candidate.hash,
            files_changed=changed_files,
            paired=paired_gain,
            created_ts=self.clock(),
            note=f"merge {candidate.hash[:12]} ({candidate.actor})",
        )
        self.versions.append(version)
        self.write_version(version)
        self.merges_since_audit += 1
        self.stats["merged"] += 1
        self.emit(
            "baseline_advanced",
            {
                "baseline": next_k,
                "parent_baseline": next_k - 1,
                "candidate": candidate.hash,
                "files_changed": changed_files,
                "paired": paired_gain,
                "guard_pending": False,
                "ticket": measurement.ticket,
                "actor": candidate.actor,
            },
        )
        if self.on_advance:
            self.on_advance(version)

    def fail(
        self, candidate: VerifiedCandidate, reason: str, paired_gain=None, note=""
    ):
        self.stats["failed"] += 1
        self.emit(
            "integration_failed",
            {
                "candidate": candidate.hash,
                "baseline": self.k,
                "paired": paired_gain,
                "reason": reason,
                "note": note,
                "actor": candidate.actor,
            },
        )

    def compose(
        self, candidate: VerifiedCandidate, base_k: int, payload_dir: Path, why: str
    ):
        """A rebase conflict: emit a compose task (the caller runs the compose agent)."""
        self.compose_task_count += 1
        self.stats["compose_tasks"] += 1
        self.emit(
            "compose_task",
            {
                "task_id": f"compose-{self.compose_task_count}",
                "candidates": [candidate.hash] + list(self.versions[self.k].candidates),
                "baseline": self.k,
                "target": f"baseline@{self.k}",
                "why": why,
                "base_dir": self.versions[base_k].dir,
                "ours_dir": self.versions[self.k].dir,
                "theirs_dir": str(payload_dir),
                "predicted": candidate.score,  # its own measured score: a fair guess for the merged version
            },
        )
        self.fail(candidate, "conflict", note=why)

    def start_audit(self, final=False):
        from_k, to_k = self.last_audited_k, self.k
        if to_k == from_k:
            self.merges_since_audit = 0
            return
        for version_k in (from_k, to_k):
            if self.versions[version_k].audit_per_seed is None:
                self.measure_audit(
                    version_k,
                    {"kind": "audit", "segment": [from_k, to_k], "final": final},
                )
                return
        self.finish_audit(from_k, to_k, final)

    def measure_audit(self, version_k: int, next_task: dict) -> bool:
        version = self.versions[version_k]
        try:
            ticket = self.submit(
                Path(version.dir),
                version_k,
                "audit",
                f"audit measurement of baseline@{version_k}",
            )
        except QuotaExhausted:
            self.audits_used = self.config.audit_max
            return False
        self.audits_used += 1
        self.stats["audits"] += 1
        self.task = dict(next_task, ticket=ticket, version_k=version_k)
        return True

    def on_audit(self, task, measurement: Measurement):
        self.versions[task["version_k"]].audit_per_seed = measurement.per_seed
        self.write_version(self.versions[task["version_k"]])
        from_k, to_k = task["segment"]
        if (
            self.versions[from_k].audit_per_seed is None
            or self.versions[to_k].audit_per_seed is None
        ):
            self.start_audit(final=task.get("final", False))
            return
        self.finish_audit(from_k, to_k, task.get("final", False))

    def threshold(self) -> float:
        return self.config.audit_k * math.sqrt(
            self.audit_sigma.get("dev", 0.0) ** 2
            + self.audit_sigma.get("audit", 0.0) ** 2
        )

    def discrepancy(self, from_k: int, to_k: int):
        from_version, to_version = self.versions[from_k], self.versions[to_k]
        dev_paired = paired(to_version.per_seed, from_version.per_seed)
        audit_paired = paired(to_version.audit_per_seed, from_version.audit_per_seed)
        return dev_paired, audit_paired, abs(dev_paired["mean"] - audit_paired["mean"])

    def finish_audit(self, from_k: int, to_k: int, final: bool):
        dev_paired, audit_paired, gap = self.discrepancy(from_k, to_k)
        audit_threshold = self.threshold()
        flagged = gap > audit_threshold + AUDIT_GAP_TOLERANCE
        self.emit(
            "audit_checked",
            {
                "segment": [from_k, to_k],
                "daily": dev_paired,
                "audit": audit_paired,
                "threshold": audit_threshold,
                "flagged": flagged,
                "final": final,
            },
        )
        self.merges_since_audit = 0
        self.last_audited_k = to_k
        if flagged:
            self.bisect(from_k, to_k, final)

    def bisect(self, low_k: int, high_k: int, final: bool):
        while high_k - low_k > 1:
            mid_k = (low_k + high_k) // 2
            if self.versions[mid_k].audit_per_seed is None:
                if self.audits_left(periodic=False) <= 0 or not self.measure_audit(
                    mid_k,
                    {
                        "kind": "bisect",
                        "low_k": low_k,
                        "high_k": high_k,
                        "final": final,
                    },
                ):
                    break  # cannot narrow further: drop the whole remaining segment
                return
            if (
                self.discrepancy(low_k, mid_k)[2]
                > self.threshold() + AUDIT_GAP_TOLERANCE
            ):
                high_k = mid_k
            else:
                low_k = mid_k
        self.revert(low_k, high_k, "audit")

    def on_bisect(self, task, measurement: Measurement):
        self.versions[task["version_k"]].audit_per_seed = measurement.per_seed
        self.bisect(task["low_k"], task["high_k"], task["final"])

    @staticmethod
    def find_culprit(
        low_k: int, high_k: int, segment_is_bad: Callable[[int, int], bool]
    ) -> int:
        """Binary search for the first merge in (low_k, high_k] that makes segment_is_bad(low_k, x) true.
        segment_is_bad(low_k, high_k) must be true. Returns the culprit version index.
        """
        while high_k - low_k > 1:
            mid_k = (low_k + high_k) // 2
            if segment_is_bad(low_k, mid_k):
                high_k = mid_k
            else:
                low_k = mid_k
        return high_k

    def revert(self, low_k: int, high_k: int, reason: str) -> Version:
        """Remove the merges that produced versions low_k+1..high_k and replay the later ones on top of low_k.
        Writes a new version (records are append-only) and quarantines the removed candidates.
        """
        top_k = self.k
        removed = [
            self.versions[i].added or self.versions[i].candidates[-1]
            for i in range(low_k + 1, high_k + 1)
        ]
        current_dir = self.workdir / "revert-tmp"
        copy_tree(Path(self.versions[low_k].dir), current_dir)
        for i in range(high_k + 1, top_k + 1):
            next_dir = self.workdir / "revert-tmp2"
            try:
                three_way(
                    Path(self.versions[i - 1].dir),
                    current_dir,
                    Path(self.versions[i].dir),
                    next_dir,
                )
            except Conflict:
                removed.append(
                    self.versions[i].added or self.versions[i].candidates[-1]
                )
                continue
            shutil.rmtree(current_dir)
            next_dir.rename(current_dir)
        next_k = top_k + 1
        version_dir = self.workdir / "baseline" / str(next_k)
        copy_tree(current_dir, version_dir)
        shutil.rmtree(current_dir, ignore_errors=True)
        same_content_version = next(
            (
                version
                for version in reversed(self.versions)
                if content_hash(Path(version.dir)) == content_hash(version_dir)
            ),
            None,
        )
        version = Version(
            next_k,
            str(version_dir),
            same_content_version.per_seed if same_content_version else [],
            same_content_version.score if same_content_version else None,
            ticket=same_content_version.ticket if same_content_version else None,
            hash=same_content_version.hash if same_content_version else None,
            candidates=[
                kept_hash
                for kept_hash in self.versions[top_k].candidates
                if kept_hash not in removed
            ],
            parent=top_k,
            removed=removed,
            audit_per_seed=(
                same_content_version.audit_per_seed if same_content_version else None
            ),
            created_ts=self.clock(),
            note=f"revert of {[removed_hash[:12] for removed_hash in removed]} ({reason})",
        )
        self.versions.append(version)
        self.write_version(version)
        self.emit(
            "baseline_reverted",
            {
                "from_baseline": top_k,
                "new_baseline": next_k,
                "removed": removed,
                "reason": reason,
            },
        )
        for removed_hash in removed:
            self.emit("quarantined", {"candidate": removed_hash, "reason": reason})
            if removed_hash in self.content_hash_by_hash:
                self.quarantined.add(self.content_hash_by_hash[removed_hash])
        self.last_audited_k = next_k
        self.merges_since_audit = 0
        if (
            same_content_version is None
        ):  # new content: measure it on dev once (outside the worker quota)
            ticket = self.submit(
                version_dir,
                next_k,
                "dev",
                f"measure reverted baseline@{next_k}",
                exempt=True,
                predicted=self.versions[low_k].score or 0.0,
            )
            self.task = {
                "kind": "revert_measure",
                "ticket": ticket,
                "version_k": next_k,
            }
        elif self.on_advance:
            self.on_advance(version)
        return version

    def on_revert_measure(self, task, measurement: Measurement):
        version = self.versions[task["version_k"]]
        version.per_seed, version.score, version.ticket, version.hash = (
            measurement.per_seed,
            measurement.score,
            measurement.ticket,
            measurement.hash,
        )
        self.write_version(version)
        if self.on_advance:
            self.on_advance(version)

    def closeout(
        self, wait: Callable[[], None] | None = None, max_steps: int = 10_000
    ) -> tuple[Version, list[dict]]:
        """Stop accepting, run the final audit, freeze the last baseline. Returns (deliverable, decisions)."""
        decisions: list[dict] = []
        self.closed = True
        for _ in range(max_steps):
            if self.task is None:
                break
            decisions += self.step()
            if self.task is not None:
                (wait or (lambda: time.sleep(self.config.poll_s)))()
        for candidate in list(self.queue):
            self.fail(candidate, "run_ended")
        self.queue.clear()
        decisions += self.step()
        if self.last_audited_k != self.k and self.audits_left(periodic=False) > 0:
            self.start_audit(final=True)
            for _ in range(max_steps):
                decisions += self.step()
                if self.task is None:
                    break
                (wait or (lambda: time.sleep(self.config.poll_s)))()
        version = self.versions[-1]
        if version.ticket is None:
            version.ticket = self.submit(
                Path(version.dir),
                version.k,
                "dev",
                f"deliverable baseline@{version.k}",
                exempt=True,
            )
            for _ in range(max_steps):
                measurement = self.measurer.result(version.ticket)
                if measurement is not None:
                    version.hash, version.per_seed, version.score = (
                        measurement.hash,
                        measurement.per_seed,
                        measurement.score,
                    )
                    break
                (wait or (lambda: time.sleep(self.config.poll_s)))()
            self.write_version(version)
        self.frozen = version
        self.emit(
            "integrator_frozen",
            {
                "baseline": version.k,
                "ticket": version.ticket,
                "hash": version.hash,
                "candidates": version.candidates,
                "stats": self.stats,
            },
        )
        decisions += self.step()
        return version, decisions
