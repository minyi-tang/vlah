"""Exercise the real uv uploader only against a loopback registry or in dry-run mode."""

import hashlib
import html
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.release_plan import Project, ReleaseManifest
from scripts.release_publish import check_dependency, check_registry, publish_pypi

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.timeout(300)


class RegistryServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.yanked: set[str] = set()
        super().__init__(("127.0.0.1", 0), RegistryHandler)


class RegistryHandler(BaseHTTPRequestHandler):
    server: RegistryServer

    def respond(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        self.server.requests.append(("GET", self.path))
        segments = self.path.strip("/").split("/")
        if segments[0] == "simple" and len(segments) == 2:
            prefix = segments[1].replace("-", "_") + "-"
            links = "\n".join(
                f'<a href="/files/{html.escape(name)}#sha256={hashlib.sha256(content).hexdigest()}">{html.escape(name)}</a>'
                for name, content in self.server.files.items()
                if name.startswith(prefix)
            )
            self.respond(200, links.encode(), "text/html")
        elif segments[0] == "pypi" and len(segments) == 4:
            project = Project.model_validate(
                {"name": segments[1], "version": segments[2]}
            )
            prefix = project.name.replace("-", "_") + "-"
            files = [
                {
                    "filename": name,
                    "digests": {"sha256": hashlib.sha256(content).hexdigest()},
                    "yanked": name in self.server.yanked,
                }
                for name, content in self.server.files.items()
                if name.startswith(prefix)
            ]
            if files:
                self.respond(
                    200,
                    json.dumps(
                        {
                            "info": project.model_dump(),
                            "urls": files,
                        }
                    ).encode(),
                    "application/json",
                )
            else:
                self.respond(404, b"not found", "text/plain")
        else:
            self.respond(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        self.server.requests.append(("POST", self.path))
        body = self.rfile.read(int(self.headers["Content-Length"]))
        message = BytesParser().parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body
        )
        for part in message.walk():
            filename = part.get_filename()
            if filename is not None:
                payload = part.get_payload(decode=True)
                assert isinstance(payload, bytes)
                if filename in self.server.files:
                    self.respond(400, b"File already exists", "text/plain")
                else:
                    self.server.files[filename] = payload
                    self.respond(200, b"OK", "text/plain")
                return
            else:
                pass
        self.respond(400, b"No file", "text/plain")


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[RegistryServer]:
    for name in (
        "UV_PUBLISH_TOKEN",
        "UV_PUBLISH_USERNAME",
        "UV_PUBLISH_PASSWORD",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    server = RegistryServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_actual_script_dry_run_never_uploads_or_requests_oidc(
    registry: RegistryServer, release_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = f"http://127.0.0.1:{registry.server_port}"
    manifest_digest = hashlib.sha256(
        (release_bundle / "release.json").read_bytes()
    ).hexdigest()
    monkeypatch.setenv("PYPI_INDEX_URL", f"{endpoint}/simple/")
    monkeypatch.setenv("PYPI_PUBLISH_URL", f"{endpoint}/legacy/")
    monkeypatch.setenv("PYPI_JSON_URL", f"{endpoint}/pypi")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.release",
            "dry-run",
            "--bundle",
            str(release_bundle),
            "--manifest-sha256",
            manifest_digest,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest = ReleaseManifest.model_validate_json(
        (release_bundle / "release.json").read_bytes()
    )
    for filename in manifest.files:
        assert f"Checking {filename}" in completed.stderr
    assert not registry.files
    assert all(method == "GET" for method, path in registry.requests)


def test_uv_retries_skip_only_identical_files(
    registry: RegistryServer, release_bundle: Path
) -> None:
    manifest = ReleaseManifest.model_validate_json(
        (release_bundle / "release.json").read_bytes()
    )
    filenames = sorted(manifest.files)
    registry.files[filenames[0]] = (release_bundle / filenames[0]).read_bytes()
    endpoint = f"http://127.0.0.1:{registry.server_port}"
    command = [
        "uv",
        "publish",
        "--no-config",
        "--trusted-publishing",
        "never",
        "--check-url",
        f"{endpoint}/simple/",
        "--publish-url",
        f"{endpoint}/legacy/",
        "--username",
        "__token__",
        "--password",
        "loopback-test-only",
        *[str(release_bundle / filename) for filename in filenames],
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert registry.files == {
        name: (release_bundle / name).read_bytes() for name in filenames
    }
    assert sum(method == "POST" for method, path in registry.requests) == 1
    repeated = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    )
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert sum(method == "POST" for method, path in registry.requests) == 1
    registry.files[filenames[0]] = b"different bytes"
    refused = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    )
    assert refused.returncode != 0
    assert sum(method == "POST" for method, path in registry.requests) == 1


@pytest.mark.parametrize("condition", ["different", "extra", "yanked"])
def test_registry_preflight_rejects_conflicts_before_invoking_uploader(
    registry: RegistryServer,
    release_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    manifest = ReleaseManifest.model_validate_json(
        (release_bundle / "release.json").read_bytes()
    )
    filename = sorted(manifest.files)[-1]
    registry.files[filename] = (release_bundle / filename).read_bytes()
    if condition == "different":
        registry.files[filename] = b"unrelated build"
    elif condition == "extra":
        registry.files["vlah_testbed-unexpected.whl"] = b"unrelated artifact"
    else:
        registry.yanked.add(filename)

    def forbidden_upload(
        command: list[str], *, check: bool, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError(
            f"Uploader must not run on conflicts: {command}, {check}, {timeout}"
        )

    monkeypatch.setattr("scripts.release_publish.subprocess.run", forbidden_upload)
    endpoint = f"http://127.0.0.1:{registry.server_port}"
    with pytest.raises(ValueError, match="Registry"):
        publish_pypi(
            release_bundle,
            manifest,
            f"{endpoint}/simple/",
            f"{endpoint}/legacy/",
            f"{endpoint}/pypi",
            dry_run=False,
        )
    assert all(method == "GET" for method, path in registry.requests)


def test_registry_completion_and_dependency_use_all_tested_hashes(
    registry: RegistryServer, release_bundle: Path
) -> None:
    manifest = ReleaseManifest.model_validate_json(
        (release_bundle / "release.json").read_bytes()
    )
    project = Project(name="vlah-testbed", version=manifest.versions["vlah-testbed"])
    endpoint = f"http://127.0.0.1:{registry.server_port}/pypi"
    for filename in manifest.files:
        registry.files[filename] = (release_bundle / filename).read_bytes()
    check_registry(project, manifest.files, endpoint, require_complete=True)
    vlah_manifest = ReleaseManifest(
        repository=manifest.repository,
        commit_sha=manifest.commit_sha,
        versions=manifest.versions,
        package="vlah",
        tag=f"v{manifest.versions['vlah']}",
        publish=True,
        files={
            name: "0" * 64
            for name in Project(
                name="vlah", version=manifest.versions["vlah"]
            ).filenames()
        },
        dependency_files=manifest.files,
    )
    check_dependency(vlah_manifest, endpoint)
    registry.files.pop(sorted(manifest.files)[0])
    with pytest.raises(ValueError, match="incomplete"):
        check_dependency(vlah_manifest, endpoint)
