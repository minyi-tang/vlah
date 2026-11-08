"""The stack assembly: herd.yaml's gaps get the stack's defaults, every worker's view
is served over HTTP with its own token, and the executor opens clones with it (#129)."""

import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from random import Random

import pytest
import yaml

from tests.fixtures.docker import FakeDockerClient
from vlah.adapters.executor.docker.executor import DockerExecutor
from vlah.adapters.store.git_http.server import GitHttpService, ServedGitHttp
from vlah.adapters.store.sqlite.store import SqliteKeyValueStore
from vlah.arms.config import PRESETS
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.problem.api import AdapterChoice, HerdConfig, JudgeConfig
from vlah.core.problem.loader import load_problem
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.runtime.assembly import RunContext
from vlah.runtime.isolation import worker_view
from vlah.runtime.local_assembly import free_port
from vlah.runtime.stack_assembly import (
    STACK_REDIS_URL,
    GitViewPublisher,
    StackAssembly,
    stack_herd_config,
)

pytestmark = pytest.mark.runtime

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP = REPO_ROOT / "examples" / "prompt_optimization" / "setup.py"
WORKER = ActorId("worker-01")


def test_the_stack_fills_in_docker_and_redis_but_keeps_explicit_choices() -> None:
    plain = HerdConfig(
        judges={"rubric": JudgeConfig(command="judge")},
        executor=AdapterChoice(kind="local", settings={"target_repository": "/t"}),
        harness=AdapterChoice(kind="scripted"),
    )
    on_stack = stack_herd_config(plain, STACK_REDIS_URL)
    assert on_stack.executor == AdapterChoice(
        kind="docker", settings={"target_repository": "/t"}
    )
    assert on_stack.stores["key_value"] == AdapterChoice(
        kind="redis", settings={"url": STACK_REDIS_URL}
    )
    explicit = plain.model_copy(
        update={"stores": {"key_value": AdapterChoice(kind="sqlite")}}
    )
    assert (
        stack_herd_config(explicit, STACK_REDIS_URL).stores["key_value"].kind
        == "sqlite"
    )


def test_the_publisher_serves_a_workers_view_and_only_that_worker_opens_it(
    tmp_path: Path,
) -> None:
    blackboard = open_blackboard(tmp_path / "runs", "r-1", "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"# Spec\n"}, "problem")
    served = ServedGitHttp.start(GitHttpService(host="127.0.0.1", port=free_port()))
    try:
        publisher = GitViewPublisher(service=served.service)
        view = worker_view(blackboard, PRESETS["B1"], "r-1", WORKER)
        url = publisher.publish("r-1", WORKER, view)
        assert url == f"{served.service.base_url}/r-1/{WORKER}/blackboard.git"
        settings = publisher.settings(WORKER)
        assert settings["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert publisher.settings(ActorId("worker-02")) == {}, "no view, no settings"
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{url}/info/refs?service=git-upload-pack")
        assert refused.value.code == 401
        request = urllib.request.Request(
            f"{url}/info/refs?service=git-upload-pack",
            headers={"Authorization": settings["GIT_CONFIG_VALUE_0"].split(": ", 1)[1]},
        )
        with urllib.request.urlopen(request) as response:
            assert b"# service=git-upload-pack" in response.read()
    finally:
        served.close()


def test_the_stack_assembly_builds_a_docker_executor_that_opens_served_views(
    tmp_path: Path,
) -> None:
    root = tmp_path / "prompt-opt"
    subprocess.run([sys.executable, str(SETUP), str(root)], check=True)
    herd_path = root / "herd.yaml"
    config: object = yaml.safe_load(herd_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    # The executor is left to the stack; the store is explicit so no Redis is needed here.
    config["stores"] = {"key_value": {"kind": "sqlite"}}
    config["harness"] = {"kind": "scripted", "settings": {}}
    herd_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    problem = load_problem(root)
    assert problem.herd_config.executor.kind == "local"
    context = RunContext(
        run_id="r-stack",
        run_root=root / ".herd" / "runs" / "r-stack",
        arm=PRESETS["B1"],
        params=problem.params,
        problem=problem,
        clock=FakeClock(),
        random=Random(0),
        duration_seconds=60.0,
    )
    assembly = StackAssembly(docker=FakeDockerClient(), executor_poll_seconds=0.5)
    components = assembly.build(context)
    try:
        executor = assembly.parts[0].executor
        assert isinstance(
            executor, DockerExecutor
        ), "the stack's workers are containers"
        assert isinstance(assembly.parts[0].key_value_store, SqliteKeyValueStore)
        publisher = components.view_publisher
        assert isinstance(publisher, GitViewPublisher)
        view = worker_view(components.blackboard, context.arm, context.run_id, WORKER)
        url = publisher.publish(context.run_id, WORKER, view)
        assert url.startswith("http://127.0.0.1:")
        assert executor.view_settings is not None
        assert "GIT_CONFIG_VALUE_0" in executor.view_settings(WORKER)
        (served,) = assembly.served_git
        assert served.uvicorn_server.started
    finally:
        assembly.close()
    assert not assembly.served_git, "close stops the git HTTP service"
