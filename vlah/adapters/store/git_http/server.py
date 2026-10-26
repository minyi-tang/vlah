"""The blackboard over HTTP, read-only, one view per worker (D67, D68).

Smart HTTP for fetching only: GET <view>/info/refs?service=git-upload-pack
advertises the refs and POST <view>/git-upload-pack answers a fetch, both by
running git upload-pack, which never writes a repository. There is no receive-pack
route, so a push is refused before git runs: two denials of a push that do not
depend on each other.

A view is one worker's repository: the run's blackboard in S2 and B0, the worker's
mirror in S1 and B1, reached as /<run id>/<agent>/blackboard.git with that worker's
bearer token. Another worker's token is 403 and no token 401, so a B1 worker cannot
clone a neighbour's mirror. The token travels as an http.extraHeader git setting in
the environment (clone_environment), never in a URL or a file.
"""

import gzip
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from vlah.core.types.ids import ActorId

UPLOAD_PACK = "git-upload-pack"
RECEIVE_PACK = "git-receive-pack"
REPOSITORY_NAME = "blackboard.git"
BEARER_PREFIX = "Bearer "
GIT_TIMEOUT_SECONDS = 120.0
SERVER_START_TIMEOUT_SECONDS = 10.0
FLUSH_PACKET = b"0000"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, max-age=0, must-revalidate",
    "Expires": "Fri, 01 Jan 1980 00:00:00 GMT",
    "Pragma": "no-cache",
}


@dataclass(frozen=True, kw_only=True)
class ViewAccess:
    """How one worker reaches its view: the URL it clones and the token that opens it."""

    url: str
    token: str


def clone_environment(token: str) -> dict[str, str]:
    """Git settings that send the token with every request, outside the URL."""
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: {BEARER_PREFIX}{token}",
    }


def packet_line(text: str) -> bytes:
    return f"{len(text) + 4:04x}{text}".encode("utf-8")


def git_upload_pack(
    repository_path: Path, body: bytes | None, protocol: str | None
) -> bytes:
    """Advertise refs (no body) or answer a fetch (with it); git never writes here."""
    arguments = ["git", "upload-pack", "--stateless-rpc"]
    if body is None:
        arguments.append("--advertise-refs")
    else:
        pass
    arguments.append(str(repository_path))
    environment = {"GIT_PROTOCOL": protocol} if protocol else {}
    completed = subprocess.run(
        arguments,
        input=body if body is not None else b"",
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **environment},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git upload-pack failed on {repository_path}: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    else:
        return completed.stdout


class GitHttpService:
    """Serves registered views; one instance per run, started with ServedGitHttp."""

    def __init__(self, *, host: str, port: int, advertised_host: str | None = None):
        self.host = host
        self.port = port
        self.advertised_host = advertised_host or host
        self.repositories: dict[tuple[str, ActorId], Path] = {}
        self.tokens: dict[tuple[str, ActorId], str] = {}
        self.lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.advertised_host}:{self.port}"

    def view_url(self, run_id: str, agent: ActorId) -> str:
        return f"{self.base_url}/{run_id}/{agent}/{REPOSITORY_NAME}"

    def register_view(
        self, run_id: str, agent: ActorId, repository_path: Path
    ) -> ViewAccess:
        """Give a worker its view; registering it again keeps its token (a restart)."""
        with self.lock:
            token = self.tokens.get((run_id, agent)) or secrets.token_hex(16)
            self.tokens[(run_id, agent)] = token
            self.repositories[(run_id, agent)] = repository_path
        return ViewAccess(url=self.view_url(run_id, agent), token=token)

    def authorized_repository(self, request: Request) -> Path | Response:
        """The view's repository when the request's token is that view's, else the error."""
        run_id = request.path_params["run_id"]
        agent = ActorId(request.path_params["agent"])
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith(BEARER_PREFIX):
            return Response("a view needs its worker's bearer token", status_code=401)
        else:
            token = authorization.removeprefix(BEARER_PREFIX)
            with self.lock:
                repository = self.repositories.get((run_id, agent))
                expected = self.tokens.get((run_id, agent))
            if repository is None:
                return Response("no such view", status_code=404)
            elif not secrets.compare_digest(token, expected or ""):
                return Response("this token does not open this view", status_code=403)
            else:
                return repository

    async def advertise(self, request: Request) -> Response:
        repository = self.authorized_repository(request)
        if isinstance(repository, Response):
            return repository
        else:
            service = request.query_params.get("service")
            if service != UPLOAD_PACK:
                return Response(
                    f"only {UPLOAD_PACK} is served; the blackboard is read-only (D68)",
                    status_code=403,
                )
            else:
                refs = await run_in_threadpool(
                    git_upload_pack,
                    repository,
                    None,
                    request.headers.get("git-protocol"),
                )
                body = packet_line(f"# service={UPLOAD_PACK}\n") + FLUSH_PACKET + refs
                return Response(
                    body,
                    media_type=f"application/x-{UPLOAD_PACK}-advertisement",
                    headers=NO_CACHE_HEADERS,
                )

    async def upload_pack(self, request: Request) -> Response:
        repository = self.authorized_repository(request)
        if isinstance(repository, Response):
            return repository
        else:
            body = await request.body()
            if request.headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            else:
                pass
            answer = await run_in_threadpool(
                git_upload_pack, repository, body, request.headers.get("git-protocol")
            )
            return Response(
                answer,
                media_type=f"application/x-{UPLOAD_PACK}-result",
                headers=NO_CACHE_HEADERS,
            )

    async def refuse_push(self, request: Request) -> Response:
        return Response("the blackboard is read-only over HTTP (D68)", status_code=403)

    def app(self) -> Starlette:
        view = f"/{{run_id}}/{{agent}}/{REPOSITORY_NAME}"
        return Starlette(
            routes=[
                Route(f"{view}/info/refs", self.advertise, methods=["GET"]),
                Route(f"{view}/{UPLOAD_PACK}", self.upload_pack, methods=["POST"]),
                Route(f"{view}/{RECEIVE_PACK}", self.refuse_push, methods=["POST"]),
            ]
        )


@dataclass(kw_only=True)
class ServedGitHttp:
    """A GitHttpService on its own thread, for the length of a run."""

    service: GitHttpService
    uvicorn_server: uvicorn.Server
    thread: threading.Thread

    @classmethod
    def start(cls, service: GitHttpService) -> "ServedGitHttp":
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                service.app(), host=service.host, port=service.port, log_level="warning"
            )
        )
        thread = threading.Thread(
            target=uvicorn_server.run, name=f"git-http-{service.port}", daemon=True
        )
        thread.start()
        deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
        while not uvicorn_server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not uvicorn_server.started:
            raise RuntimeError(
                f"the git HTTP server on {service.base_url} did not start"
            )
        else:
            return cls(service=service, uvicorn_server=uvicorn_server, thread=thread)

    def close(self) -> None:
        self.uvicorn_server.should_exit = True
        self.thread.join(SERVER_START_TIMEOUT_SECONDS)
