"""HTTP client for the slow-eval service plus candidate packaging (manifest + zip)."""

from __future__ import annotations

import base64
import io
import os
import time
import zipfile
from pathlib import Path

import httpx
import yaml

from testbed.problem_api import iter_candidate_files


class ServiceError(RuntimeError):
    def __init__(self, code, body):
        super().__init__(f"{code}: {body}")
        self.code = code
        self.body = body


def write_manifest(
    d: Path,
    actor,
    predicted,
    note="",
    parents=(),
    direction=None,
    target="baseline@0",
    run_id=None,
    arm=None,
    **extra,
) -> dict:
    m = {
        "actor": actor,
        "predicted_score": float(predicted),
        "note": note or "",
        "parents": list(parents or []),
        "direction": direction,
        "target": target,
        "run_id": run_id,
        "arm": arm,
        "created": time.time(),
    }
    m.update(extra)
    (Path(d) / "manifest.yaml").write_text(yaml.safe_dump(m, sort_keys=True))
    return m


def zip_candidate(d: Path) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, p in iter_candidate_files(d):
            zf.write(p, rel)
        mf = Path(d) / "manifest.yaml"
        if mf.exists():
            zf.write(mf, "manifest.yaml")
    data = buf.getvalue()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("candidate zip exceeds 5 MB")
    return base64.b64encode(data).decode()


class SlowClient:
    """Works against a URL or, for tests, any httpx-compatible client (e.g. FastAPI TestClient)."""

    def __init__(
        self,
        base_url: str | None,
        run_id: str,
        actor: str = "anon",
        http=None,
        key: str | None = None,
        token: str | None = None,
    ):
        self.run_id = run_id
        self.actor = actor
        self.key = key
        self.token = token
        self.http = http or httpx.Client(base_url=base_url, timeout=3600)

    @classmethod
    def from_env(cls):
        url = os.environ.get("TB_SLOW_URL")
        run = os.environ.get("TB_RUN_ID")
        if not url or not run:
            raise SystemExit("tb: TB_SLOW_URL and TB_RUN_ID must be set")
        return cls(
            url,
            run,
            os.environ.get("TB_ACTOR", "anon"),
            key=os.environ.get("TB_SYSTEM_KEY"),
            token=os.environ.get("TB_TOKEN"),
        )

    def as_actor(self, actor):
        return SlowClient(None, self.run_id, actor, http=self.http, key=self.key)

    def _h(self):
        h = {"X-TB-Run": self.run_id, "X-TB-Actor": self.actor}
        if self.key:
            h["X-TB-Key"] = self.key
        if self.token:
            h["X-TB-Token"] = self.token
        return h

    def _check(self, r):
        try:
            body = r.json()
        except Exception:
            body = {"text": r.text}
        if r.status_code >= 400:
            raise ServiceError(r.status_code, body)
        return body

    def create_run(self, arm, problem, n, overrides=None, actor_tokens=None):
        return self._check(
            self.http.post(
                "/runs",
                json={
                    "run_id": self.run_id,
                    "arm": arm,
                    "problem": problem,
                    "N": n,
                    "overrides": overrides or {},
                    "actor_tokens": actor_tokens or {},
                },
                headers=self._h(),
            )
        )

    def submit(self, zip_b64, dataset="dev"):
        return self._check(
            self.http.post(
                "/submit",
                json={"zip_b64": zip_b64, "dataset": dataset},
                headers=self._h(),
            )
        )

    def submit_dir(self, d: Path, predicted, dataset="dev", **manifest):
        write_manifest(
            d, actor=self.actor, predicted=predicted, run_id=self.run_id, **manifest
        )
        return self.submit(zip_candidate(d), dataset=dataset)

    def status(self, ticket):
        return self._check(self.http.get(f"/status/{ticket}", headers=self._h()))

    def history(self):
        return self._check(self.http.get("/history", headers=self._h()))

    def meta(self):
        return self._check(self.http.get("/meta", headers=self._h()))

    def calibration(self):
        return self._check(self.http.get("/calibration", headers=self._h()))

    def post_events(self, events):
        return self._check(
            self.http.post("/events", json={"events": events}, headers=self._h())
        )

    def events(self, since=0):
        return self._check(
            self.http.get("/events", params={"since": since}, headers=self._h())
        )

    def candidate(self, h):
        return self._check(self.http.get(f"/candidate/{h}", headers=self._h()))

    def final(self, **body):
        return self._check(self.http.post("/final", json=body, headers=self._h()))

    def final_check(self, ticket, dataset=None):
        return self._check(
            self.http.post(
                "/final_check",
                json={"ticket": ticket, "dataset": dataset},
                headers=self._h(),
            )
        )


def unzip_to(zip_b64: str, dest: Path) -> Path:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(zip_b64))) as zf:
        for info in zf.infolist():
            if (
                info.is_dir()
                or info.filename.startswith("/")
                or ".." in Path(info.filename).parts
            ):
                continue
            out = dest / info.filename
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(info))
    return dest
