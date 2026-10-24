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

MAX_ZIP_BYTES = 5 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 3600


class ServiceError(RuntimeError):
    def __init__(self, status_code, body):
        super().__init__(f"{status_code}: {body}")
        self.status_code = status_code
        self.body = body


def write_manifest(
    candidate_dir: Path,
    actor,
    predicted,
    note="",
    parents=(),
    direction=None,
    target="baseline@0",
    run_id=None,
    arm=None,
    **extra_fields,
) -> dict:
    manifest = {
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
    manifest.update(extra_fields)
    (Path(candidate_dir) / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=True)
    )
    return manifest


def zip_candidate(candidate_dir: Path) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path, path in iter_candidate_files(candidate_dir):
            archive.write(path, relative_path)
        manifest_path = Path(candidate_dir) / "manifest.yaml"
        if manifest_path.exists():
            archive.write(manifest_path, "manifest.yaml")
    zip_bytes = buffer.getvalue()
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise ValueError("candidate zip exceeds 5 MB")
    return base64.b64encode(zip_bytes).decode()


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
        self.http = http or httpx.Client(
            base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS
        )

    @classmethod
    def from_env(cls):
        slow_url = os.environ.get("TB_SLOW_URL")
        run_id = os.environ.get("TB_RUN_ID")
        if not slow_url or not run_id:
            raise SystemExit("tb: TB_SLOW_URL and TB_RUN_ID must be set")
        return cls(
            slow_url,
            run_id,
            os.environ.get("TB_ACTOR", "anon"),
            key=os.environ.get("TB_SYSTEM_KEY"),
            token=os.environ.get("TB_TOKEN"),
        )

    def as_actor(self, actor):
        return SlowClient(None, self.run_id, actor, http=self.http, key=self.key)

    def headers(self):
        header_fields = {"X-TB-Run": self.run_id, "X-TB-Actor": self.actor}
        if self.key:
            header_fields["X-TB-Key"] = self.key
        if self.token:
            header_fields["X-TB-Token"] = self.token
        return header_fields

    def parse_response(self, response):
        try:
            body = response.json()
        except Exception:
            body = {"text": response.text}
        if response.status_code >= 400:
            raise ServiceError(response.status_code, body)
        return body

    def create_run(self, arm, problem, worker_count, overrides=None, actor_tokens=None):
        return self.parse_response(
            self.http.post(
                "/runs",
                json={
                    "run_id": self.run_id,
                    "arm": arm,
                    "problem": problem,
                    "N": worker_count,
                    "overrides": overrides or {},
                    "actor_tokens": actor_tokens or {},
                },
                headers=self.headers(),
            )
        )

    def submit(self, zip_b64, dataset="dev"):
        return self.parse_response(
            self.http.post(
                "/submit",
                json={"zip_b64": zip_b64, "dataset": dataset},
                headers=self.headers(),
            )
        )

    def submit_dir(
        self, candidate_dir: Path, predicted, dataset="dev", **manifest_fields
    ):
        write_manifest(
            candidate_dir,
            actor=self.actor,
            predicted=predicted,
            run_id=self.run_id,
            **manifest_fields,
        )
        return self.submit(zip_candidate(candidate_dir), dataset=dataset)

    def fast(self, zip_b64, seed: int, target: str):
        return self.parse_response(
            self.http.post(
                "/fast",
                json={"zip_b64": zip_b64, "seed": seed, "target": target},
                headers=self.headers(),
            )
        )

    def status(self, ticket):
        return self.parse_response(
            self.http.get(f"/status/{ticket}", headers=self.headers())
        )

    def history(self):
        return self.parse_response(self.http.get("/history", headers=self.headers()))

    def meta(self):
        return self.parse_response(self.http.get("/meta", headers=self.headers()))

    def calibration(self):
        return self.parse_response(
            self.http.get("/calibration", headers=self.headers())
        )

    def post_events(self, events):
        return self.parse_response(
            self.http.post("/events", json={"events": events}, headers=self.headers())
        )

    def events(self, since=0):
        return self.parse_response(
            self.http.get("/events", params={"since": since}, headers=self.headers())
        )

    def candidate(self, candidate_hash):
        return self.parse_response(
            self.http.get(f"/candidate/{candidate_hash}", headers=self.headers())
        )

    def final(self, **body_fields):
        return self.parse_response(
            self.http.post("/final", json=body_fields, headers=self.headers())
        )

    def final_check(self, ticket, dataset=None):
        return self.parse_response(
            self.http.post(
                "/final_check",
                json={"ticket": ticket, "dataset": dataset},
                headers=self.headers(),
            )
        )


def unzip_to(zip_b64: str, destination_dir: Path) -> Path:
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(zip_b64))) as archive:
        for member in archive.infolist():
            if (
                member.is_dir()
                or member.filename.startswith("/")
                or ".." in Path(member.filename).parts
            ):
                continue
            out_path = destination_dir / member.filename
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(archive.read(member))
    return destination_dir
