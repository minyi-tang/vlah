"""Slow-tier evaluation service core (spec 11.3). Framework-free so tests can drive it with a
manual clock; app.py wraps it in FastAPI.

Storage (single machine; sqlite stands in for Redis as the spec allows):
  <data>/tb.sqlite                     runs + tickets (queue, quota, registry)
  <data>/runs/<run_id>/events.jsonl    the recorder (append-only)
  <data>/runs/<run_id>/cands/<chash>/  extracted candidates
  <data>/blobs/<sha>.zip               submitted zips
  <data>/cache/                        evaluation cache for deterministic problems
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from testbed.problem_api import (
    candidate_hash,
    content_hash,
    fast_pool,
    load_problem,
    resolve_problem_dir,
    sd,
)
from testbed.recorder.log import Recorder

MAX_ZIP = 5 * 1024 * 1024
# Fast requests evaluating at once; a burst waits here instead of forking a process per site.
FAST_CONCURRENT_EVALUATIONS = 2
# Per-site counters and job waits stay on the service: they would show a worker which hidden
# mechanisms fired (issue #113).
FAST_EXTRA_HIDDEN_KEYS = ("waits", "mechanisms")
SYSTEM_ACTORS = {"runner", "integrator", "composer", "slow_eval"}
EXEMPT_CAP = 6
# Event types the testbed itself writes. Over HTTP only the system key may post them.
RESERVED_EVENTS = {
    "calibration",
    "submit",
    "result",
    "quota_rejected",
    "final",
    "final_check",
    "actor_final",
    "anomaly",
    "run_end",
    "baseline_advanced",
    "baseline_reverted",
    "integration_failed",
    "interaction_found",
    "audit_checked",
    "compose_task",
    "quarantined",
    "integrator_frozen",
    "stale_target",
    "agent_restart",
    "unsafe_no_sandbox",
}


class RealClock:
    def now(self) -> float:
        return time.time()


class ManualClock:
    def __init__(self, t0: float = 1_760_800_000.0):
        self.t = float(t0)

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


class ApiError(Exception):
    def __init__(self, code: int, body: dict):
        super().__init__(body)
        self.code = code
        self.body = body


def _problem_fingerprint(root: Path, judge_sites: Path | None = None) -> str:
    """Hash of the problem's code and settings, the judge's hidden site profiles included."""
    h = hashlib.sha256()
    roots = [Path(root)] + ([judge_sites] if judge_sites is not None else [])
    for base in roots:
        for p in sorted(base.rglob("*")):
            if (
                p.is_file()
                and "__pycache__" not in p.parts
                and p.suffix in (".py", ".yaml", ".yml", ".json")
            ):
                h.update(str(p.relative_to(base)).encode() + p.read_bytes())
    return h.hexdigest()[:16]


class FetchedRows:
    """The rows of one statement, read in full while the connection lock was held."""

    def __init__(self, rows: list, lastrowid, rowcount: int):
        self.rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list:
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class SerializedConnection:
    """One sqlite connection used from several threads (the request threads, the publisher and
    the evaluation pool), with every statement run and read under one lock.

    sqlite's connection object is not safe to step from two threads at once: with
    check_same_thread=False alone, concurrent calls raise "bad parameter or other API misuse"
    or return NULL for a column that was just written (issue #106). Reading the rows before the
    lock is released keeps a cursor from being stepped while another thread runs a statement.
    """

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self.connection = connection
        self.lock = lock

    def execute(self, sql: str, parameters=()) -> FetchedRows:
        with self.lock:
            cursor = self.connection.execute(sql, parameters)
            return FetchedRows(cursor.fetchall(), cursor.lastrowid, cursor.rowcount)

    def executescript(self, sql: str) -> None:
        with self.lock:
            self.connection.executescript(sql)

    def commit(self) -> None:
        with self.lock:
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()


class SlowEval:
    def __init__(
        self, data_dir: Path, clock=None, inline: bool = False, concurrency: int = 2
    ):
        self.data = Path(data_dir)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "blobs").mkdir(exist_ok=True)
        (self.data / "cache").mkdir(exist_ok=True)
        self.clock = clock or RealClock()
        self.inline = inline
        self.lock = threading.RLock()
        connection = sqlite3.connect(
            self.data / "tb.sqlite", check_same_thread=False, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        self.db = SerializedConnection(connection, self.lock)
        self.db.executescript(
            """
        CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, arm TEXT, problem TEXT, n INTEGER,
            knobs TEXT, t0 REAL, cal TEXT);
        CREATE TABLE IF NOT EXISTS tickets (ticket TEXT PRIMARY KEY, seq INTEGER, run_id TEXT, actor TEXT,
            hash TEXT, chash TEXT, dataset TEXT, state TEXT, ts_submit REAL, visible_at REAL, predicted REAL,
            note TEXT, direction TEXT, parents TEXT, target TEXT, result TEXT, published INTEGER DEFAULT 0,
            dup_of TEXT, qclass TEXT, blob TEXT, manifest TEXT);
        CREATE INDEX IF NOT EXISTS t_run ON tickets(run_id);
        """
        )
        if "tokens" not in {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN tokens TEXT")
        self.pool = None if inline else ThreadPoolExecutor(max_workers=concurrency)
        kf = self.data / "system.key"
        if not kf.exists():
            import secrets

            kf.write_text(secrets.token_hex(16))
            kf.chmod(0o600)
        self.system_key = kf.read_text().strip()
        self._problems: dict[str, object] = {}
        self._fps: dict[str, str] = {}
        self.fast_evaluation_slots = threading.Semaphore(FAST_CONCURRENT_EVALUATIONS)
        # (problem name, public seed) -> baseline fast score, for runs whose calibration lacks it
        self.baseline_fast_scores: dict[tuple[str, int], float | None] = {}

    # ------------------------------------------------------------------ helpers
    def now(self):
        return self.clock.now()

    def problem(self, name):
        if name not in self._problems:
            prob = load_problem(name)
            self._problems[name] = prob
            hidden = getattr(prob, "hidden", None)
            self._fps[name] = _problem_fingerprint(
                resolve_problem_dir(name),
                hidden / "sites" if hidden is not None else None,
            )
        return self._problems[name]

    def run_row(self, run_id):
        r = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            raise ApiError(404, {"reason": "unknown_run"})
        return r

    def knobs(self, run_id):
        return json.loads(self.run_row(run_id)["knobs"])

    def recorder(self, run_id) -> Recorder:
        r = self.run_row(run_id)
        return Recorder(self.data / "runs" / run_id / "events.jsonl", run_id, r["arm"])

    def cand_dir(self, run_id, chash) -> Path:
        return self.data / "runs" / run_id / "cands" / chash

    # ------------------------------------------------------------------ evaluation (cached)
    def _evaluate(
        self, prob_name: str, cand: Path, dataset: str, n_seeds: int | None, tier="slow"
    ) -> dict:
        prob = self.problem(prob_name)
        seeds = prob.seeds(dataset)
        if n_seeds:
            seeds = seeds[:n_seeds]
        deterministic = getattr(prob, "deterministic", True)
        key = hashlib.sha256(
            json.dumps(
                [
                    prob_name,
                    self._fps[prob_name],
                    content_hash(cand),
                    dataset,
                    seeds,
                    tier,
                ]
            ).encode()
        ).hexdigest()
        cf = self.data / "cache" / f"{key}.json"
        if deterministic and cf.exists():
            return json.loads(cf.read_text())
        agg = prob.aggregate(prob.evaluate_many(cand, seeds, tier))
        agg = json.loads(json.dumps(agg))  # plain JSON types
        if deterministic:
            cf.write_text(json.dumps(agg))
        return agg

    def _probes(self, prob_name: str, cand: Path) -> list[str]:
        key = hashlib.sha256(
            json.dumps(
                [prob_name, self._fps[prob_name], content_hash(cand), "probes"]
            ).encode()
        ).hexdigest()
        cf = self.data / "cache" / f"{key}.json"
        if cf.exists():
            return json.loads(cf.read_text())
        v = self.problem(prob_name).probes(cand)
        out = sorted(k for k, x in v.items() if x)
        cf.write_text(json.dumps(out))
        return out

    # ------------------------------------------------------------------ runs
    def create_run(self, body: dict) -> dict:
        run_id = body.get("run_id")
        if not run_id:
            raise ApiError(400, {"reason": "missing_run_id"})
        prob_name = body.get("problem", "sched")
        prob = self.problem(prob_name)
        knobs = prob.knobs()
        knobs.update(body.get("overrides") or {})
        n = int(body.get("N", 1))
        with self.lock:
            if self.db.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone():
                raise ApiError(409, {"reason": "run_exists"})
            t0 = self.now()
            self.db.execute(
                "INSERT INTO runs (run_id, arm, problem, n, knobs, t0, cal, tokens) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    body.get("arm"),
                    prob_name,
                    n,
                    json.dumps(knobs),
                    t0,
                    None,
                    json.dumps(body.get("actor_tokens") or {}),
                ),
            )
        ns = knobs.get("slow_seeds")
        b_dev = self._evaluate(prob_name, prob.baseline_dir(), "dev", ns)
        r_dev = self._evaluate(prob_name, prob.reference_dir(), "dev", ns)
        b_aud = self._evaluate(prob_name, prob.baseline_dir(), "audit", ns)
        r_aud = self._evaluate(prob_name, prob.reference_dir(), "audit", ns)
        bf = self._evaluate(prob_name, prob.baseline_dir(), "public", None, tier="fast")
        b_fast = {str(s): v for s, v in zip(prob.public_seeds, bf["per_seed"])}

        def sig(r, b):
            d = [x - y for x, y in zip(r["per_seed"], b["per_seed"])]
            return sd(d) / math.sqrt(len(d)) if len(d) > 1 else 0.0

        cal = {
            "baseline_slow": b_dev,
            "reference_slow": r_dev,
            "baseline_fast": b_fast,
            "baseline_audit": b_aud,
            "reference_audit": r_aud,
            "audit_sigma": {"dev": sig(r_dev, b_dev), "audit": sig(r_aud, b_aud)},
            "knobs": knobs,
            "problem": prob_name,
            "N": n,
            "arm": body.get("arm"),
            "ingredients": list(prob.ingredients),
            "constraint_rule": getattr(prob, "constraint_rule", ""),
        }
        with self.lock:
            self.db.execute(
                "UPDATE runs SET cal=? WHERE run_id=?", (json.dumps(cal), run_id)
            )
        self.recorder(run_id).write("calibration", cal, ts=t0, actor="slow_eval")
        return {"run_id": run_id, "t0": t0, "knobs": knobs}

    def check_actor(self, run_id, actor, token, trusted: bool):
        """When the run registered per-agent tokens, an untrusted caller may act only as the actor whose
        token it holds (DECISIONS I14). Trusted callers (the system key) may act for anyone.
        """
        if trusted:
            return
        tokens = json.loads(self.run_row(run_id)["tokens"] or "{}")
        if tokens and (actor not in tokens or tokens[actor] != token):
            raise ApiError(403, {"reason": "unknown actor or wrong X-TB-Token"})

    def calibration(self, run_id) -> dict:
        r = self.run_row(run_id)
        return json.loads(r["cal"]) if r["cal"] else {}

    # ------------------------------------------------------------------ quota
    def _window(self, run_id, t, length):
        t0 = self.run_row(run_id)["t0"]
        w = int((t - t0) // length)
        return t0 + w * length, t0 + (w + 1) * length

    def _count(self, run_id, qclass, actor=None, since=None, dataset=None):
        q = "SELECT COUNT(*) FROM tickets WHERE run_id=? AND qclass=?"
        args = [run_id, qclass]
        if actor is not None:
            q += " AND actor=?"
            args.append(actor)
        if since is not None:
            q += " AND ts_submit>=?"
            args.append(since)
        if dataset is not None:
            q += " AND dataset=?"
            args.append(dataset)
        return self.db.execute(q, args).fetchone()[0]

    def _share(self, run_id):
        k = self.knobs(run_id)
        n = max(1, self.run_row(run_id)["n"])
        qph = k["quota_per_hour"]
        window_h = max(1, math.ceil(n / qph))
        per_window = 1
        total = math.ceil(k["quota_total"] / n)
        return window_h * k.get("hour_s", 3600), per_window, total

    def _quota_class(self, run_id, actor, dataset, manifest, now):
        """Return the quota class to charge, or raise 429/403."""
        k = self.knobs(run_id)
        hour = k.get("hour_s", 3600)
        if dataset == "audit":
            if actor not in ("runner", "integrator"):
                raise ApiError(403, {"reason": "audit_restricted"})
            if self._count(run_id, "audit") >= k.get("audit_total", 10):
                raise ApiError(429, {"reason": "quota", "next_slot_at": None})
            return "audit"
        if dataset == "judge":
            if actor != "runner":
                raise ApiError(403, {"reason": "judge_restricted"})
            if self._count(run_id, "judge") >= k.get("judge_total", 5):
                raise ApiError(429, {"reason": "quota", "next_slot_at": None})
            return "judge"
        if actor == "runner" or (
            actor == "integrator" and manifest.get("quota") == "exempt"
        ):
            if self._count(run_id, "exempt") >= EXEMPT_CAP:
                raise ApiError(429, {"reason": "quota", "next_slot_at": None})
            return "exempt"
        if actor in ("integrator", "composer"):
            lo, hi = self._window(run_id, now, hour)
            used_total = self._count(run_id, "integrator")
            if used_total >= k.get("quota_integrator_total", 16):
                raise ApiError(429, {"reason": "quota", "next_slot_at": None})
            if self._count(run_id, "integrator", since=lo) >= k.get(
                "quota_integrator_per_hour", 1
            ):
                raise ApiError(429, {"reason": "quota", "next_slot_at": hi})
            return "integrator"
        if k.get("per_actor_share"):
            length, per_window, total = self._share(run_id)
            lo, hi = self._window(run_id, now, length)
            if self._count(run_id, "worker", actor=actor) >= total:
                raise ApiError(429, {"reason": "quota", "next_slot_at": None})
            if self._count(run_id, "worker", actor=actor, since=lo) >= per_window:
                raise ApiError(429, {"reason": "quota", "next_slot_at": hi})
            return "worker"
        lo, hi = self._window(run_id, now, hour)
        if self._count(run_id, "worker") >= k["quota_total"]:
            raise ApiError(429, {"reason": "quota", "next_slot_at": None})
        if self._count(run_id, "worker", since=lo) >= k["quota_per_hour"]:
            raise ApiError(429, {"reason": "quota", "next_slot_at": hi})
        return "worker"

    # ------------------------------------------------------------------ submit
    def _unzip(self, zip_bytes: bytes, dest: Path):
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            raise ApiError(400, {"reason": "invalid_candidate", "detail": "not a zip"})
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ApiError(
                    400, {"reason": "invalid_candidate", "detail": f"bad path {name}"}
                )
            if info.is_dir():
                continue
            out = tmp / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(info))
        return tmp

    def submit(
        self, run_id: str, body: dict, trusted: bool = True, token: str | None = None
    ) -> dict:
        run = self.run_row(run_id)
        self.publish_due()
        raw = body.get("zip_b64") or ""
        try:
            zbytes = base64.b64decode(raw)
        except Exception:
            raise ApiError(400, {"reason": "invalid_candidate", "detail": "bad base64"})
        if len(zbytes) > MAX_ZIP:
            raise ApiError(400, {"reason": "too_large"})
        dataset = body.get("dataset") or "dev"
        if dataset not in ("dev", "audit", "judge"):
            raise ApiError(400, {"reason": "invalid_candidate", "detail": "dataset"})
        # One staging directory per call: two workers submitting the same bytes at the same
        # time must not unpack into, and delete, each other's directory (#106).
        staging = (
            self.data / "runs" / run_id / "cands" / ("staging-" + uuid.uuid4().hex)
        )
        tmp = self._unzip(zbytes, staging)
        try:
            mf = tmp / "manifest.yaml"
            manifest = (yaml.safe_load(mf.read_text()) or {}) if mf.exists() else {}
            pred = manifest.get("predicted_score")
            if not isinstance(pred, (int, float)) or isinstance(pred, bool):
                raise ApiError(400, {"reason": "missing_prediction"})
            prob = self.problem(run["problem"])
            err = prob.validate_candidate(tmp)
            if err:
                raise ApiError(400, {"reason": "invalid_candidate", "detail": err})
            target = str(manifest.get("target") or "baseline@0")
            h = candidate_hash(tmp, target)
            ch = content_hash(tmp)
            dest = self.cand_dir(run_id, h)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp.rename(dest)
            except OSError:
                if not dest.exists():  # not the same candidate arriving twice at once
                    raise
                shutil.rmtree(tmp, ignore_errors=True)
        except ApiError:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        blob = self.data / "blobs" / (hashlib.sha256(zbytes).hexdigest() + ".zip")
        if not blob.exists():
            blob.write_bytes(zbytes)
        actor = str(manifest.get("actor") or "anon")
        if actor in SYSTEM_ACTORS and not trusted:
            raise ApiError(
                403, {"reason": f"actor {actor!r} is reserved for the testbed"}
            )
        self.check_actor(run_id, actor, token, trusted)
        now = self.now()
        k = self.knobs(run_id)
        with self.lock:
            q = "SELECT * FROM tickets WHERE run_id=? AND hash=? AND dataset=? AND dup_of IS NULL AND state != 'failed'"
            args = [run_id, h, dataset]
            if k.get("per_actor_share") and actor not in SYSTEM_ACTORS:
                q += " AND actor=?"
                args.append(actor)
            orig = self.db.execute(q + " ORDER BY seq LIMIT 1", args).fetchone()
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM tickets"
            ).fetchone()[0]
            ticket = f"t-{seq:06d}"
            parents = manifest.get("parents") or []
            common = (ticket, seq, run_id, actor, h, ch, dataset)
            rec = self.recorder(run_id)
            ev_data = {
                "ticket": ticket,
                "hash": h,
                "predicted_score": float(pred),
                "parents": parents,
                "direction": manifest.get("direction"),
                "dataset": dataset,
                "target": target,
                "note": manifest.get("note") or "",
            }
            if orig is not None:
                self.db.execute(
                    "INSERT INTO tickets (ticket, seq, run_id, actor, hash, chash, dataset, state, ts_submit, visible_at,"
                    " predicted, note, direction, parents, target, dup_of, qclass, blob, manifest)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    common
                    + (
                        "dup",
                        now,
                        orig["visible_at"],
                        float(pred),
                        manifest.get("note"),
                        manifest.get("direction"),
                        json.dumps(parents),
                        target,
                        orig["ticket"],
                        "dup",
                        str(blob),
                        json.dumps(manifest),
                    ),
                )
                rec.write(
                    "submit",
                    dict(ev_data, duplicate=True, of=orig["ticket"]),
                    ts=now,
                    actor=actor,
                )
                return {"ticket": ticket, "duplicate": True, "of": orig["ticket"]}
            try:
                qclass = self._quota_class(run_id, actor, dataset, manifest, now)
            except ApiError as e:
                if e.code == 429:
                    rec.write(
                        "quota_rejected",
                        {"actor": actor, "hash": h, "dataset": dataset},
                        ts=now,
                        actor=actor,
                    )
                raise
            visible = now + float(k.get("visible_delay_s", 600))
            self.db.execute(
                "INSERT INTO tickets (ticket, seq, run_id, actor, hash, chash, dataset, state, ts_submit, visible_at,"
                " predicted, note, direction, parents, target, dup_of, qclass, blob, manifest)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                common
                + (
                    "queued",
                    now,
                    visible,
                    float(pred),
                    manifest.get("note"),
                    manifest.get("direction"),
                    json.dumps(parents),
                    target,
                    None,
                    qclass,
                    str(blob),
                    json.dumps(manifest),
                ),
            )
            rec.write(
                "submit",
                dict(ev_data, duplicate=False, quota_class=qclass),
                ts=now,
                actor=actor,
            )
            position = self.db.execute(
                "SELECT COUNT(*) FROM tickets WHERE state IN ('queued','running')"
            ).fetchone()[0]
        if self.inline:
            self._run_ticket(ticket)
        else:
            self.pool.submit(self._run_ticket, ticket)
        return {"ticket": ticket, "position": position, "visible_at": visible}

    # ------------------------------------------------------------------ fast tier (issue #113)
    def baseline_fast(self, run_id: str, problem_name: str, seed: int) -> float | None:
        """The baseline's fast score on a public seed: from the run's calibration when it has it,
        else evaluated once per process."""
        calibration_scores = self.calibration(run_id).get("baseline_fast") or {}
        if str(seed) in calibration_scores:
            return calibration_scores[str(seed)]
        else:
            pass
        cache_key = (problem_name, seed)
        if cache_key not in self.baseline_fast_scores:
            prob = self.problem(problem_name)
            self.baseline_fast_scores[cache_key] = prob.evaluate(
                prob.baseline_dir(), "fast", seed
            )["score"]
        else:
            pass
        return self.baseline_fast_scores[cache_key]

    def fast(self, run_id: str, body: dict, actor: str) -> dict:
        """Evaluate a candidate on the public tier for one public seed and record one fast_run
        event. No ticket, no quota, no visibility delay: the fast tier is free and unlimited, it
        only moves from the worker's machine to the service so the worker copy needs no mechanisms.
        """
        run = self.run_row(run_id)
        try:
            zip_bytes = base64.b64decode(body.get("zip_b64") or "")
        except Exception:
            raise ApiError(400, {"reason": "invalid_candidate", "detail": "bad base64"})
        if len(zip_bytes) > MAX_ZIP:
            raise ApiError(400, {"reason": "too_large"})
        else:
            pass
        prob = self.problem(run["problem"])
        seed = body.get("seed")
        if (
            not isinstance(seed, int)
            or isinstance(seed, bool)
            or seed not in prob.public_seeds
        ):
            raise ApiError(
                400,
                {
                    "reason": "invalid_seed",
                    "detail": f"only public seeds {list(prob.public_seeds)} are allowed",
                },
            )
        else:
            pass
        staging = self.data / "runs" / run_id / "fast" / ("staging-" + uuid.uuid4().hex)
        candidate_dir = self._unzip(zip_bytes, staging)
        started_at = self.now()
        try:
            manifest_path = candidate_dir / "manifest.yaml"
            manifest = (
                (yaml.safe_load(manifest_path.read_text()) or {})
                if manifest_path.exists()
                else {}
            )
            validation_error = prob.validate_candidate(candidate_dir)
            if validation_error:
                raise ApiError(
                    400, {"reason": "invalid_candidate", "detail": validation_error}
                )
            else:
                pass
            target = str(body.get("target") or manifest.get("target") or "baseline@0")
            submitted_hash = candidate_hash(candidate_dir, target)
            with self.fast_evaluation_slots, fast_pool():
                result = prob.evaluate(candidate_dir, "fast", seed)
                baseline_score = self.baseline_fast(run_id, run["problem"], seed)
        finally:
            shutil.rmtree(candidate_dir, ignore_errors=True)
        self.recorder(run_id).write(
            "fast_run",
            {
                "hash": submitted_hash,
                "seed": seed,
                "score": result["score"],
                "baseline_score": baseline_score,
            },
            ts=self.now(),
            actor=actor,
        )
        return {
            "hash": submitted_hash,
            "seed": seed,
            "score": result["score"],
            "baseline_score": baseline_score,
            "per_item": result["per_item"],
            "extra": {
                key: value
                for key, value in result["extra"].items()
                if key not in FAST_EXTRA_HIDDEN_KEYS
            },
            "elapsed_s": round(self.now() - started_at, 1),
        }

    def _run_ticket(self, ticket):
        with self.lock:
            t = self.db.execute(
                "SELECT * FROM tickets WHERE ticket=?", (ticket,)
            ).fetchone()
            self.db.execute(
                "UPDATE tickets SET state='running' WHERE ticket=?", (ticket,)
            )
            run = self.run_row(t["run_id"])
        # Everything from here on is inside the try: a failure before the evaluation used to leave
        # the ticket 'running' forever, with the exception swallowed by the pool future (#106).
        try:
            knobs = json.loads(run["knobs"])
            cal = json.loads(run["cal"])
            cand = self.cand_dir(t["run_id"], t["hash"])
            agg = self._evaluate(
                run["problem"], cand, t["dataset"], knobs.get("slow_seeds")
            )
            prob = self.problem(run["problem"])
            base = (
                cal["baseline_slow"]
                if t["dataset"] == "dev"
                else (
                    cal["baseline_audit"]
                    if t["dataset"] == "audit"
                    else self._evaluate(
                        run["problem"],
                        prob.baseline_dir(),
                        t["dataset"],
                        knobs.get("slow_seeds"),
                    )
                )
            )
            ok = (
                bool(prob.constraint_ok(agg, base))
                if agg["score"] is not None
                else False
            )
            viol = (
                prob.constraint_violations(agg, base)
                if hasattr(prob, "constraint_violations")
                else []
            )
            ings = self._probes(run["problem"], cand)
            result = dict(agg, constraint_ok=ok, violations=viol, ingredients=ings)
            state = "done"
        except Exception as e:  # evaluation infrastructure failure
            result = {
                "score": None,
                "ci": [None, None],
                "n": 0,
                "per_seed": [],
                "per_item": [],
                "extra": {"error": f"{type(e).__name__}: {e}"},
                "constraint_ok": False,
                "ingredients": [],
            }
            state = "failed"
        with self.lock:
            visible = max(t["visible_at"], self.now())
            self.db.execute(
                "UPDATE tickets SET state=?, result=?, visible_at=? WHERE ticket=?",
                (state, json.dumps(result), visible, ticket),
            )
        self.publish_due()

    def publish_due(self):
        now = self.now()
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM tickets WHERE published=0 AND dup_of IS NULL AND state IN ('done','failed')"
                " AND visible_at<=? ORDER BY visible_at, seq",
                (now,),
            ).fetchall()
            for t in rows:
                res = json.loads(t["result"])
                data = {
                    "ticket": t["ticket"],
                    "hash": t["hash"],
                    "score": res["score"],
                    "ci": res["ci"],
                    "constraint_ok": res["constraint_ok"],
                    "ingredients": res["ingredients"],
                    "per_seed": res.get("per_seed"),
                    "per_item": res.get("per_item"),
                    "violations": res.get("violations", []),
                    "dataset": t["dataset"],
                    "target": t["target"],
                    "predicted_score": t["predicted"],
                    "state": t["state"],
                }
                rec = self.recorder(t["run_id"])
                rec.write("result", data, ts=t["visible_at"], actor=t["actor"])
                self.db.execute(
                    "UPDATE tickets SET published=1 WHERE ticket=?", (t["ticket"],)
                )
                cal = self.calibration(t["run_id"])
                ref = (cal.get("reference_slow") or {}).get("score")
                if (
                    t["dataset"] == "dev"
                    and res["score"] is not None
                    and ref is not None
                ):
                    ci = cal["reference_slow"].get("ci") or [ref, ref]
                    margin = max((ci[1] or ref) - (ci[0] or ref), 1e-9)
                    if res["score"] > ref + margin:
                        rec.write(
                            "anomaly",
                            {
                                "ticket": t["ticket"],
                                "score": res["score"],
                                "reference_score": ref,
                                "reason": "slow-tier score far above the reference policy; review",
                            },
                            ts=t["visible_at"],
                            actor="slow_eval",
                        )

    # ------------------------------------------------------------------ reads
    def _visible_result(self, t):
        if t["dup_of"]:
            t = self.db.execute(
                "SELECT * FROM tickets WHERE ticket=?", (t["dup_of"],)
            ).fetchone()
        state = t["state"]
        if state in ("done", "failed") and t["visible_at"] > self.now():
            state = "running"
        out = {"state": state}
        if state in ("done", "failed"):
            res = json.loads(t["result"])
            res.get("extra", {}).pop("waits", None)
            out["result"] = res
        return out

    def status(self, run_id, ticket, actor=None) -> dict:
        self.run_row(run_id)
        self.publish_due()
        t = self.db.execute(
            "SELECT * FROM tickets WHERE ticket=? AND run_id=?", (ticket, run_id)
        ).fetchone()
        if t is None:
            raise ApiError(404, {"reason": "unknown_ticket"})
        if (
            self.knobs(run_id).get("per_actor_share")
            and actor not in SYSTEM_ACTORS
            and t["actor"] != actor
        ):
            raise ApiError(404, {"reason": "unknown_ticket"})
        return dict(self._visible_result(t), ticket=ticket, hash=t["hash"])

    def history(self, run_id, actor=None) -> dict:
        self.publish_due()
        k = self.knobs(run_id)
        now = self.now()
        share = k.get("per_actor_share") and actor not in SYSTEM_ACTORS
        rows = self.db.execute(
            "SELECT * FROM tickets WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        items = []
        for t in rows:
            if share and t["actor"] != actor:
                continue
            vis = self._visible_result(t)
            res = vis.get("result") or {}
            items.append(
                {
                    "ticket": t["ticket"],
                    "actor": t["actor"],
                    "ts_submit": t["ts_submit"],
                    "visible_at": t["visible_at"],
                    "hash": t["hash"],
                    "predicted_score": t["predicted"],
                    "direction": t["direction"],
                    "note": t["note"],
                    "dataset": t["dataset"],
                    "duplicate_of": t["dup_of"],
                    "score": res.get("score"),
                    "ci": res.get("ci"),
                    "constraint_ok": res.get("constraint_ok"),
                }
            )
        if share:
            length, per_window, total = self._share(run_id)
            lo, _ = self._window(run_id, now, length)
            quota = {
                "per_hour": per_window * 3600 / length,
                "per_window": per_window,
                "window_s": length,
                "used_this_window": self._count(
                    run_id, "worker", actor=actor, since=lo
                ),
                "total": total,
                "used_total": self._count(run_id, "worker", actor=actor),
            }
        else:
            lo, _ = self._window(run_id, now, k.get("hour_s", 3600))
            quota = {
                "per_hour": k["quota_per_hour"],
                "window_s": k.get("hour_s", 3600),
                "used_this_hour": self._count(run_id, "worker", since=lo),
                "total": k["quota_total"],
                "used_total": self._count(run_id, "worker"),
            }
        return {"quota": quota, "items": items}

    def meta(self, run_id) -> dict:
        cal = self.calibration(run_id)
        prob = self.problem(self.run_row(run_id)["problem"])
        k = cal["knobs"]
        b = {kk: v for kk, v in cal["baseline_slow"].items()}
        b.get("extra", {}).pop("waits", None)
        return {
            "baseline": b,
            "public_seeds": prob.public_seeds,
            "constraint_rule": cal.get("constraint_rule"),
            "quota_per_hour": k["quota_per_hour"],
            "quota_total": k["quota_total"],
            "visible_delay_s": k["visible_delay_s"],
            "slow_seeds": k["slow_seeds"],
            "per_actor_share": bool(k.get("per_actor_share")),
        }

    def events(self, run_id, since: int = 0) -> dict:
        p = self.data / "runs" / run_id / "events.jsonl"
        self.publish_due()
        if not p.exists():
            return {"events": [], "next": since}
        lines = p.read_text().splitlines()
        return {
            "events": [json.loads(x) for x in lines[since:] if x.strip()],
            "next": len(lines),
        }

    def post_events(
        self, run_id, events: list[dict], actor=None, trusted: bool = True
    ) -> dict:
        rec = self.recorder(run_id)
        now = self.now()
        if not trusted:
            if (actor or "anon") in SYSTEM_ACTORS:
                raise ApiError(
                    403, {"reason": f"actor {actor!r} is reserved for the testbed"}
                )
            bad = sorted({ev.get("type") for ev in events} & RESERVED_EVENTS)
            if bad:
                raise ApiError(
                    403,
                    {"reason": f"event types {bad} are written by the testbed only"},
                )
        n = 0
        for ev in events:
            data = ev.get("data") or {}
            extra = {}
            if "ts" in ev:
                extra["client_ts"] = ev["ts"]
            who = (ev.get("actor") or actor) if trusted else actor
            rec.write(ev["type"], data, ts=now, actor=who, **extra)
            n += 1
        return {"appended": n}

    def candidate(self, run_id, h) -> dict:
        t = self.db.execute(
            "SELECT * FROM tickets WHERE run_id=? AND hash=? ORDER BY seq LIMIT 1",
            (run_id, h),
        ).fetchone()
        if t is None:
            raise ApiError(404, {"reason": "unknown_candidate"})
        return {
            "hash": h,
            "zip_b64": base64.b64encode(Path(t["blob"]).read_bytes()).decode(),
            "manifest": json.loads(t["manifest"] or "{}"),
            "ticket": t["ticket"],
        }

    def ticket_row(self, run_id, ticket):
        t = self.db.execute(
            "SELECT * FROM tickets WHERE ticket=? AND run_id=?", (ticket, run_id)
        ).fetchone()
        if t is None:
            raise ApiError(404, {"reason": "unknown_ticket"})
        return t

    # ------------------------------------------------------------------ final
    def final(self, run_id, body: dict, actor: str) -> dict:
        self.publish_due()
        if body.get("ticket"):
            t = self.ticket_row(run_id, body["ticket"])
        elif body.get("hash"):
            t = self.db.execute(
                "SELECT * FROM tickets WHERE run_id=? AND hash=? AND dataset='dev' ORDER BY seq DESC LIMIT 1",
                (run_id, body["hash"]),
            ).fetchone()
            if t is None:
                raise ApiError(404, {"reason": "unknown_candidate"})
        else:
            raise ApiError(400, {"reason": "need hash or ticket"})
        if t["dup_of"]:
            t = self.ticket_row(run_id, t["dup_of"])
        rec = self.recorder(run_id)
        if actor != "runner":
            rec.write(
                "actor_final",
                {"actor": actor, "hash": t["hash"], "ticket": t["ticket"]},
                ts=self.now(),
                actor=actor,
            )
            return {"recorded": "actor_final", "ticket": t["ticket"]}
        if t["state"] not in ("done", "failed"):
            # no result yet: evaluate once outside the quota, synchronously
            self._run_ticket(t["ticket"])
            t = self.ticket_row(run_id, t["ticket"])
        if t["visible_at"] > self.now():
            with self.lock:
                self.db.execute(
                    "UPDATE tickets SET visible_at=? WHERE ticket=?",
                    (self.now(), t["ticket"]),
                )
            self.publish_due()
        rec.write(
            "final",
            {"hash": t["hash"], "ticket": t["ticket"]},
            ts=self.now(),
            actor="runner",
        )
        return {"recorded": "final", "ticket": t["ticket"], "hash": t["hash"]}

    def final_check(self, run_id, body: dict) -> dict:
        t = self.ticket_row(run_id, body["ticket"])
        run = self.run_row(run_id)
        prob = self.problem(run["problem"])
        allowed = getattr(prob, "final_check_dataset", "public")
        dataset = body.get("dataset") or allowed
        if dataset != allowed:
            raise ApiError(400, {"reason": f"final_check dataset must be {allowed}"})
        cand = self.cand_dir(run_id, t["hash"])
        if dataset == "public":
            # public seeds, but the slow tier's horizon, so it is comparable with the official score
            agg = prob.aggregate(prob.evaluate_many(cand, prob.public_seeds, "slow"))
        else:
            agg = self._evaluate(run["problem"], cand, dataset, None)
        data = {
            "dataset": dataset,
            "score": agg["score"],
            "ci": agg["ci"],
            "ticket": t["ticket"],
            "hash": t["hash"],
            "n": agg["n"],
        }
        self.recorder(run_id).write("final_check", data, ts=self.now(), actor="runner")
        return data

    def close(self):
        if self.pool:
            self.pool.shutdown(wait=True)
