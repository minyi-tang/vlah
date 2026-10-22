"""L0-12 (v2.2 §14.1). Owner: W1-06 (#16).

The worker role reads private:// → denied; a worker pushes to the repository → denied (D63, D68; the in-memory adapters must simulate permissions too).
"""

import subprocess
from pathlib import Path

import pytest

from tests.fixtures.tools import make_tools_harness
from vlah.adapters.memory.store import MemoryVersionedStore
from vlah.adapters.store.git.store import GitVersionedStore
from vlah.arms.config import PRESETS
from vlah.core.types.ids import ActorId
from vlah.ports.errors import PushRejectedError, ReadPermissionError

pytestmark = [pytest.mark.l0, pytest.mark.tools]

WORKER = ActorId("worker-01")
GIT_IDENTITY = ("-c", "user.name=worker-01", "-c", "user.email=worker-01@herd")


def git(clone: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(clone), *GIT_IDENTITY, *arguments],
        capture_output=True,
        check=False,
    )


def test_l0_12_worker_permissions(tmp_path: Path) -> None:
    """The worker role reads private:// → denied; a worker pushes to the repository → denied (D63, D68; the in-memory adapters must simulate permissions too)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.tools.register_agent(WORKER, tmp_path)
    secret = harness.objects.put("judge/hidden_seeds", b"12345", True)
    assert "not readable" in str(
        harness.tools.repo_read(WORKER, secret).result["error"]
    )
    with pytest.raises(ReadPermissionError):
        harness.objects.get(secret, WORKER)

    memory_store = MemoryVersionedStore(name="blackboard", push_token="tool-server")
    with pytest.raises(PushRejectedError):
        memory_store.commit(
            "main", {"claims/worker-01/c.md": b"x"}, WORKER, "push", "a guess"
        )

    git_store = GitVersionedStore(
        repository_path=tmp_path / "blackboard.git", push_token="tool-server"
    )
    git_store.commit(
        "main", {"spec.md": b"spec"}, ActorId("human"), "spec", "tool-server"
    )
    clone = tmp_path / "worker-clone"
    subprocess.run(
        ["git", "clone", "-q", git_store.clone_url(True), str(clone)], check=True
    )
    (clone / "claims" / "worker-01").mkdir(parents=True)
    (clone / "claims" / "worker-01" / "c-worker-01-00001.md").write_text("pushed")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "push around the tool server")
    push = git(clone, "push", "-q", "origin", "HEAD:main")
    assert push.returncode != 0 and b"push token" in push.stderr
