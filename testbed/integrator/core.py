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
    d = [x - y for x, y in zip(candidate_per_seed, baseline_per_seed)]
    return {"mean": mean(d) if d else 0.0, "se": se(d), "n_items": len(d), "n_reps": 0}


def target_index(target: str | None) -> int | None:
    m = re.fullmatch(r"baseline@(\d+)", str(target or "baseline@0"))
    return int(m.group(1)) if m else None


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
        """calibration: baseline_slow and baseline_audit Aggs (per_seed, score, ci) and audit_sigma {dev, audit}."""
        self.cfg = config
        self.wd = Path(workdir)
        (self.wd / "baseline").mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.measurer = measurer
        self.on_advance = on_advance
        self.sigma = calibration.get("audit_sigma") or {"dev": 0.0, "audit": 0.0}
        v0dir = self.wd / "baseline" / "0"
        copy_tree(baseline_dir, v0dir)
        base = calibration["baseline_slow"]
        self.versions = [
            Version(
                0,
                str(v0dir),
                base["per_seed"],
                base["score"],
                base.get("ci"),
                audit_per_seed=(calibration.get("baseline_audit") or {}).get(
                    "per_seed"
                ),
                created_ts=clock(),
                note="baseline@0",
            )
        ]
        self.write_version(self.versions[0])
        self.queue: deque = deque()
        self.out: list[dict] = []
        self.task: dict | None = None
        self.wait_until = 0.0
        self.quota_exhausted = False
        self.merges_since_audit = 0
        self.last_audit = 0
        self.audit_used = 0
        self.closed = False
        self.frozen: Version | None = None
        self.compose_ids = 0
        self.quarantined: set[str] = set()  # content hashes removed by an audit
        self.files_by_hash: dict[str, str] = {}
        self.stats = {
            "remeasures": 0,
            "audits": 0,
            "compose_tasks": 0,
            "merged": 0,
            "failed": 0,
        }

    # ------------------------------------------------------------------ basics
    @property
    def k(self) -> int:
        return len(self.versions) - 1

    def current(self) -> Version:
        return self.versions[-1]

    def emit(self, type_: str, data: dict):
        self.out.append(dict(data, type=type_))

    def write_version(self, v: Version):
        (self.wd / "baseline" / f"{v.k}.json").write_text(
            json.dumps(v.record(), indent=1, default=str)
        )
        (self.wd / "baseline" / f"{v.k}.testbed.json").write_text(
            json.dumps(asdict(v), indent=1, default=str)
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
            self.merges_since_audit >= self.cfg.audit_every
            and self.audits_left(periodic=True) > 0
        ):
            return "now"
        if not self.queue:
            return None
        if any(target_index(c.target) == self.k for c in self.queue):
            return "now"
        if self.quota_exhausted:
            return None
        return "now" if now >= self.wait_until else self.wait_until

    def step(self) -> list[dict]:
        """Advance by at most one action; return the decisions made (as event-shaped dicts)."""
        if self.task is not None:
            m = self.measurer.result(self.task["ticket"])
            if m is not None:
                task, self.task = self.task, None
                getattr(self, "on_" + task["kind"])(task, m)
        elif not self.closed:
            if (
                self.merges_since_audit >= self.cfg.audit_every
                and self.audits_left(periodic=True) > 0
            ):
                self.start_audit()
            else:
                self.process_next()
        out, self.out = self.out, []
        return out

    def audits_left(self, periodic: bool) -> int:
        left = self.cfg.audit_max - self.audit_used
        return left - self.cfg.audit_reserve if periodic else left

    def submit(
        self,
        d: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents=(),
        exempt=False,
        predicted: float | None = None,
    ) -> str:
        return self.measurer.submit(
            d,
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

    # ------------------------------------------------------------------ one candidate
    def process_next(self):
        blocked = self.clock() < self.wait_until or self.quota_exhausted
        for i, c in enumerate(self.queue):
            if blocked and target_index(c.target) != self.k:
                continue
            del self.queue[i]
            self.integrate(c)
            return

    def integrate(self, c: VerifiedCandidate):
        t = target_index(c.target)
        if t is None or t > self.k:
            self.fail(c, "invalid_target")
            return
        src = Path(c.files)
        self.files_by_hash[c.hash] = content_hash(src)
        if self.files_by_hash[c.hash] in self.quarantined:
            self.fail(c, "quarantined")
            return
        claimed = t
        t = self.effective_base(src, t)
        if t != claimed:
            self.emit(
                "stale_target",
                {
                    "candidate": c.hash,
                    "claimed": claimed,
                    "effective": t,
                    "actor": c.actor,
                },
            )
        out = self.wd / "rebased" / f"{c.hash[:16]}@{self.k}"
        try:
            changed = three_way(
                Path(self.versions[t].dir), Path(self.versions[self.k].dir), src, out
            )
        except Conflict as e:
            self.compose(c, t, src, str(e))
            return
        earlier = paired(c.per_seed, self.versions[t].per_seed)
        if t == self.k:
            self.decide(
                c,
                out,
                changed,
                earlier,
                Measurement(c.ticket, c.hash, c.per_seed, c.score, True),
            )
            return
        if not changed:
            self.fail(
                c,
                "below_1se",
                paired={"mean": 0.0, "se": 0.0, "n_items": 0, "n_reps": 0},
                note="no change after rebase",
            )
            return
        try:
            ticket = self.submit(
                out,
                self.k,
                "dev",
                f"remeasure {c.hash[:12]} on baseline@{self.k}",
                parents=[c.hash],
                predicted=(self.versions[self.k].score or 0.0) + earlier["mean"],
            )
        except QuotaExhausted as q:
            self.queue.appendleft(c)
            if q.next_slot_at:
                self.wait_until = q.next_slot_at
            else:
                self.quota_exhausted = True
            return
        self.stats["remeasures"] += 1
        self.task = {
            "kind": "remeasure",
            "ticket": ticket,
            "cand": c,
            "out": str(out),
            "changed": changed,
            "earlier": earlier,
            "base_k": self.k,
        }

    def effective_base(self, src: Path, t: int) -> int:
        """The version a candidate was really built on: the one nearest to it by content, whatever its label
        says (agents keep editing work/ after a merge, and copy old directories). Ties go to the claimed
        version, then to the older one: an older base can only cause a conflict (a compose task), while a
        newer base the candidate lacks would silently drop that version's changes."""
        distances = [distance(src, Path(v.dir)) for v in self.versions]
        nearest = min(distances)
        tied = [j for j, d in enumerate(distances) if d == nearest]
        return t if t in tied else tied[0]

    def on_remeasure(self, task, m: Measurement):
        c = task["cand"]
        if (
            task["base_k"] != self.k
        ):  # the baseline moved under us (revert): try again later
            self.queue.appendleft(c)
            return
        m.constraint_ok = m.constraint_ok and m.score is not None
        self.decide(
            c, Path(task["out"]), task["changed"], task["earlier"], m, remeasured=True
        )

    def decide(
        self,
        c: VerifiedCandidate,
        out: Path,
        changed,
        earlier,
        m: Measurement,
        remeasured=False,
    ):
        base = self.versions[self.k]
        pr = paired(m.per_seed, base.per_seed) if m.score is not None else None
        if pr is not None and remeasured:
            pr["n_reps"] = 1
            thr = self.cfg.interaction_k * math.sqrt(earlier["se"] ** 2 + pr["se"] ** 2)
            if earlier["mean"] - pr["mean"] > thr:
                self.emit(
                    "interaction_found",
                    {
                        "candidate": c.hash,
                        "baseline": self.k,
                        "earlier": earlier,
                        "remeasured": pr,
                        "threshold": thr,
                        "overlaps_with": list(base.candidates),
                    },
                )
        if not m.constraint_ok or pr is None:
            self.fail(c, "constraint", paired=pr)
            return
        if not (pr["mean"] > self.cfg.accept_se * pr["se"]):
            self.fail(c, "below_1se", paired=pr)
            return
        k1 = self.k + 1
        vdir = self.wd / "baseline" / str(k1)
        copy_tree(out, vdir)
        v = Version(
            k1,
            str(vdir),
            m.per_seed,
            m.score,
            ticket=m.ticket,
            hash=m.hash,
            candidates=base.candidates + [c.hash],
            parent=self.k,
            added=c.hash,
            files_changed=changed,
            paired=pr,
            created_ts=self.clock(),
            note=f"merge {c.hash[:12]} ({c.actor})",
        )
        self.versions.append(v)
        self.write_version(v)
        self.merges_since_audit += 1
        self.stats["merged"] += 1
        self.emit(
            "baseline_advanced",
            {
                "baseline": k1,
                "parent_baseline": k1 - 1,
                "candidate": c.hash,
                "files_changed": changed,
                "paired": pr,
                "guard_pending": False,
                "ticket": m.ticket,
                "actor": c.actor,
            },
        )
        if self.on_advance:
            self.on_advance(v)

    def fail(self, c: VerifiedCandidate, reason: str, paired=None, note=""):
        self.stats["failed"] += 1
        self.emit(
            "integration_failed",
            {
                "candidate": c.hash,
                "baseline": self.k,
                "paired": paired,
                "reason": reason,
                "note": note,
                "actor": c.actor,
            },
        )

    def compose(self, c: VerifiedCandidate, t: int, src: Path, why: str):
        """A rebase conflict: emit a compose task (the caller runs the compose agent)."""
        self.compose_ids += 1
        self.stats["compose_tasks"] += 1
        self.emit(
            "compose_task",
            {
                "task_id": f"compose-{self.compose_ids}",
                "candidates": [c.hash] + list(self.versions[self.k].candidates),
                "baseline": self.k,
                "target": f"baseline@{self.k}",
                "why": why,
                "base_dir": self.versions[t].dir,
                "ours_dir": self.versions[self.k].dir,
                "theirs_dir": str(src),
                "predicted": c.score,  # its own measured score: a fair guess for the merged version
            },
        )
        self.fail(c, "conflict", note=why)

    # ------------------------------------------------------------------ audit (D72)
    def start_audit(self, final=False):
        j, k = self.last_audit, self.k
        if k == j:
            self.merges_since_audit = 0
            return
        for idx in (j, k):
            if self.versions[idx].audit_per_seed is None:
                self.measure_audit(
                    idx, {"kind": "audit", "segment": [j, k], "final": final}
                )
                return
        self.finish_audit(j, k, final)

    def measure_audit(self, idx: int, then: dict) -> bool:
        v = self.versions[idx]
        try:
            ticket = self.submit(
                Path(v.dir), idx, "audit", f"audit measurement of baseline@{idx}"
            )
        except QuotaExhausted:
            self.audit_used = self.cfg.audit_max
            return False
        self.audit_used += 1
        self.stats["audits"] += 1
        self.task = dict(then, ticket=ticket, idx=idx)
        return True

    def on_audit(self, task, m: Measurement):
        self.versions[task["idx"]].audit_per_seed = m.per_seed
        self.write_version(self.versions[task["idx"]])
        j, k = task["segment"]
        if (
            self.versions[j].audit_per_seed is None
            or self.versions[k].audit_per_seed is None
        ):
            self.start_audit(final=task.get("final", False))
            return
        self.finish_audit(j, k, task.get("final", False))

    def threshold(self) -> float:
        return self.cfg.audit_k * math.sqrt(
            self.sigma.get("dev", 0.0) ** 2 + self.sigma.get("audit", 0.0) ** 2
        )

    def discrepancy(self, a: int, b: int):
        va, vb = self.versions[a], self.versions[b]
        dev = paired(vb.per_seed, va.per_seed)
        aud = paired(vb.audit_per_seed, va.audit_per_seed)
        return dev, aud, abs(dev["mean"] - aud["mean"])

    def finish_audit(self, j: int, k: int, final: bool):
        dev, aud, gap = self.discrepancy(j, k)
        thr = self.threshold()
        flagged = gap > thr + 1e-9
        self.emit(
            "audit_checked",
            {
                "segment": [j, k],
                "daily": dev,
                "audit": aud,
                "threshold": thr,
                "flagged": flagged,
                "final": final,
            },
        )
        self.merges_since_audit = 0
        self.last_audit = k
        if flagged:
            self.bisect(j, k, final)

    def bisect(self, lo: int, hi: int, final: bool):
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.versions[mid].audit_per_seed is None:
                if self.audits_left(periodic=False) <= 0 or not self.measure_audit(
                    mid, {"kind": "bisect", "lo": lo, "hi": hi, "final": final}
                ):
                    break  # cannot narrow further: drop the whole remaining segment
                return
            if self.discrepancy(lo, mid)[2] > self.threshold() + 1e-9:
                hi = mid
            else:
                lo = mid
        self.revert(lo, hi, "audit")

    def on_bisect(self, task, m: Measurement):
        self.versions[task["idx"]].audit_per_seed = m.per_seed
        self.bisect(task["lo"], task["hi"], task["final"])

    # ------------------------------------------------------------------ primitives for guard batches
    @staticmethod
    def find_culprit(
        lo: int, hi: int, segment_is_bad: Callable[[int, int], bool]
    ) -> int:
        """Binary search for the first merge in (lo, hi] that makes segment_is_bad(lo, x) true.
        segment_is_bad(lo, hi) must be true. Returns the culprit version index."""
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if segment_is_bad(lo, mid):
                hi = mid
            else:
                lo = mid
        return hi

    def revert(self, lo: int, hi: int, reason: str) -> Version:
        """Remove the merges that produced versions lo+1..hi and replay the later ones on top of lo.
        Writes a new version (records are append-only) and quarantines the removed candidates.
        """
        top = self.k
        removed = [
            self.versions[i].added or self.versions[i].candidates[-1]
            for i in range(lo + 1, hi + 1)
        ]
        cur = self.wd / "revert-tmp"
        copy_tree(Path(self.versions[lo].dir), cur)
        for i in range(hi + 1, top + 1):
            nxt = self.wd / "revert-tmp2"
            try:
                three_way(
                    Path(self.versions[i - 1].dir), cur, Path(self.versions[i].dir), nxt
                )
            except Conflict:
                removed.append(
                    self.versions[i].added or self.versions[i].candidates[-1]
                )
                continue
            shutil.rmtree(cur)
            nxt.rename(cur)
        k1 = top + 1
        vdir = self.wd / "baseline" / str(k1)
        copy_tree(cur, vdir)
        shutil.rmtree(cur, ignore_errors=True)
        same = next(
            (
                v
                for v in reversed(self.versions)
                if content_hash(Path(v.dir)) == content_hash(vdir)
            ),
            None,
        )
        v = Version(
            k1,
            str(vdir),
            same.per_seed if same else [],
            same.score if same else None,
            ticket=same.ticket if same else None,
            hash=same.hash if same else None,
            candidates=[x for x in self.versions[top].candidates if x not in removed],
            parent=top,
            removed=removed,
            audit_per_seed=same.audit_per_seed if same else None,
            created_ts=self.clock(),
            note=f"revert of {[h[:12] for h in removed]} ({reason})",
        )
        self.versions.append(v)
        self.write_version(v)
        self.emit(
            "baseline_reverted",
            {
                "from_baseline": top,
                "new_baseline": k1,
                "removed": removed,
                "reason": reason,
            },
        )
        for h in removed:
            self.emit("quarantined", {"candidate": h, "reason": reason})
            if h in self.files_by_hash:
                self.quarantined.add(self.files_by_hash[h])
        self.last_audit = k1
        self.merges_since_audit = 0
        if (
            same is None
        ):  # new content: measure it on dev once (outside the worker quota)
            ticket = self.submit(
                vdir,
                k1,
                "dev",
                f"measure reverted baseline@{k1}",
                exempt=True,
                predicted=self.versions[lo].score or 0.0,
            )
            self.task = {"kind": "revert_measure", "ticket": ticket, "k": k1}
        elif self.on_advance:
            self.on_advance(v)
        return v

    def on_revert_measure(self, task, m: Measurement):
        v = self.versions[task["k"]]
        v.per_seed, v.score, v.ticket, v.hash = m.per_seed, m.score, m.ticket, m.hash
        self.write_version(v)
        if self.on_advance:
            self.on_advance(v)

    # ------------------------------------------------------------------ closeout (D79)
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
                (wait or (lambda: time.sleep(self.cfg.poll_s)))()
        for c in list(self.queue):
            self.fail(c, "run_ended")
        self.queue.clear()
        decisions += self.step()
        if self.last_audit != self.k and self.audits_left(periodic=False) > 0:
            self.start_audit(final=True)
            for _ in range(max_steps):
                decisions += self.step()
                if self.task is None:
                    break
                (wait or (lambda: time.sleep(self.cfg.poll_s)))()
        v = self.versions[-1]
        if v.ticket is None:
            v.ticket = self.submit(
                Path(v.dir), v.k, "dev", f"deliverable baseline@{v.k}", exempt=True
            )
            for _ in range(max_steps):
                m = self.measurer.result(v.ticket)
                if m is not None:
                    v.hash, v.per_seed, v.score = m.hash, m.per_seed, m.score
                    break
                (wait or (lambda: time.sleep(self.cfg.poll_s)))()
            self.write_version(v)
        self.frozen = v
        self.emit(
            "integrator_frozen",
            {
                "baseline": v.k,
                "ticket": v.ticket,
                "hash": v.hash,
                "candidates": v.candidates,
                "stats": self.stats,
            },
        )
        decisions += self.step()
        return v, decisions
