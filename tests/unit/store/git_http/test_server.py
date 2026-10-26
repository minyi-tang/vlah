"""The blackboard over HTTP: a worker reads its own view and nothing else, and nobody pushes (#129)."""

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.tools import free_port
from vlah.adapters.store.git_http.server import (
    GitHttpService,
    ServedGitHttp,
    clone_environment,
)
from vlah.arms.config import PRESETS
from vlah.core.blackboard.blackboard import MirrorBlackboard, StoreBlackboard
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.ids import ActorId
from vlah.runtime.isolation import worker_view

pytestmark = pytest.mark.blackboard

RUN_ID = "r-http"
HUMAN = ActorId("human")
WORKER_ONE = ActorId("worker-01")
WORKER_TWO = ActorId("worker-02")
GIT_TIMEOUT_SECONDS = 60.0


def git(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GIT_TERMINAL_PROMPT": "0",
            **(environment or {}),
        },
    )


def repository_of(view: StoreBlackboard | MirrorBlackboard) -> Path:
    store = view.store
    assert hasattr(store, "repository_path")
    return getattr(store, "repository_path")


@pytest.fixture
def served(tmp_path: Path) -> Iterator[ServedGitHttp]:
    service = GitHttpService(host="127.0.0.1", port=free_port())
    server = ServedGitHttp.start(service)
    yield server
    server.close()


def test_a_worker_clones_and_fetches_its_own_view_with_its_token(
    tmp_path: Path, served: ServedGitHttp
) -> None:
    blackboard = open_blackboard(tmp_path / "runs", RUN_ID, "blackboard")
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n"}, "problem")
    assert isinstance(blackboard, StoreBlackboard)
    access = served.service.register_view(RUN_ID, WORKER_ONE, repository_of(blackboard))
    clone = tmp_path / "clone"
    for protocol in ("2", "0"):
        completed = git(
            "-c",
            f"protocol.version={protocol}",
            "clone",
            "-q",
            access.url,
            str(clone / protocol),
            environment=clone_environment(access.token),
        )
        assert completed.returncode == 0, completed.stderr
        assert (clone / protocol / "spec.md").read_text() == "# Spec\n"
    blackboard.write(HUMAN, {"approaches/a-01.md": b"go\n"}, "approach")
    completed = git(
        "-C",
        str(clone / "2"),
        "fetch",
        "-q",
        access.url,
        "HEAD",
        environment=clone_environment(access.token),
    )
    assert completed.returncode == 0, completed.stderr
    git("-C", str(clone / "2"), "reset", "-q", "--hard", "FETCH_HEAD")
    assert (clone / "2" / "approaches" / "a-01.md").read_text() == "go\n"
    again = served.service.register_view(RUN_ID, WORKER_ONE, repository_of(blackboard))
    assert again.token == access.token, "a restarted session keeps its token"


def test_no_token_wrong_token_and_push_are_refused(
    tmp_path: Path, served: ServedGitHttp
) -> None:
    blackboard = open_blackboard(tmp_path / "runs", RUN_ID, "blackboard")
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n"}, "problem")
    assert isinstance(blackboard, StoreBlackboard)
    access = served.service.register_view(RUN_ID, WORKER_ONE, repository_of(blackboard))
    without = git("ls-remote", access.url)
    # On a 401 git asks for a username, which the disabled prompt turns into a failure.
    assert without.returncode != 0 and (
        "401" in without.stderr or "Username" in without.stderr
    ), without.stderr
    wrong = git("ls-remote", access.url, environment=clone_environment("not-the-token"))
    assert wrong.returncode != 0 and "403" in wrong.stderr
    clone = tmp_path / "clone"
    assert (
        git(
            "clone",
            "-q",
            access.url,
            str(clone),
            environment=clone_environment(access.token),
        ).returncode
        == 0
    )
    (clone / "claims").mkdir()
    (clone / "claims" / "forged.md").write_text("x\n")
    git("-C", str(clone), "add", ".")
    git(
        "-C",
        str(clone),
        "-c",
        "user.name=w",
        "-c",
        "user.email=w@w",
        "commit",
        "-qm",
        "forge",
    )
    push = git(
        "-C",
        str(clone),
        "push",
        access.url,
        "HEAD:refs/heads/main",
        environment=clone_environment(access.token),
    )
    assert push.returncode != 0, "a worker pushed over HTTP (D68)"
    assert "403" in push.stderr or "read-only" in push.stderr, push.stderr
    assert blackboard.list_paths("claims/", None) == ()


def test_b1_workers_cannot_read_each_others_views(
    tmp_path: Path, served: ServedGitHttp
) -> None:
    """Each B1 worker has its own mirror and its own token; a neighbour's URL is a
    403 with my token, and my own view holds only my claims (D67)."""
    blackboard = open_blackboard(tmp_path / "runs", RUN_ID, "blackboard")
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n"}, "problem")
    arm = PRESETS["B1"]
    views = {
        agent: worker_view(blackboard, arm, RUN_ID, agent)
        for agent in (WORKER_ONE, WORKER_TWO)
    }
    for agent, view in views.items():
        assert isinstance(view, MirrorBlackboard)
    accesses = {
        agent: served.service.register_view(RUN_ID, agent, repository_of(view))
        for agent, view in views.items()
        if isinstance(view, MirrorBlackboard)
    }
    blackboard.write(
        WORKER_TWO,
        {
            f"claims/{WORKER_TWO}/c-{WORKER_TWO}-00001.md": (
                b"---\nid: c-worker-02-00001\ntype: insight\nstatus: proposed\n"
                b"approach: a-01\nauthor: worker-02\nevidence:\n- path: spec.md\n---\n"
                b"mine\n"
            )
        },
        "a claim",
    )
    neighbour = git(
        "ls-remote",
        accesses[WORKER_TWO].url,
        environment=clone_environment(accesses[WORKER_ONE].token),
    )
    assert neighbour.returncode != 0 and "403" in neighbour.stderr
    clone = tmp_path / "clone-one"
    completed = git(
        "clone",
        "-q",
        accesses[WORKER_ONE].url,
        str(clone),
        environment=clone_environment(accesses[WORKER_ONE].token),
    )
    assert completed.returncode == 0, completed.stderr
    assert (clone / "spec.md").is_file()
    assert not (clone / "claims").exists(), "another worker's claim reached a B1 view"
