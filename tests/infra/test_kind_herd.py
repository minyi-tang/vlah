"""The Kubernetes executor on the kind cluster (#131): the executor's contract, the
worker boundary from inside a pod, and the LLM-free herd with two scripted workers.
Needs the cluster vlah stack --kind up makes; skipped when it does not answer."""

import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from random import Random

import pytest
import yaml
from pydantic import JsonValue

from tests.fixtures.executor import TASK, make_target_repository
from vlah.adapters.executor.kubernetes.client import KubectlCli
from vlah.adapters.executor.kubernetes.egress import IMAGE_PYTHON, run_label
from vlah.adapters.executor.kubernetes.executor import KubernetesExecutor, PodResources
from vlah.adapters.executor.local.sessions import ClaimLedger
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.core.blackboard.api import Blackboard
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.problem.loader import load_problem
from vlah.core.types.claims import Claim, parse_claim
from vlah.core.types.clock import SystemClock
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.ports.executor import AgentSpec, StartPack
from vlah.runtime.herd import build_herd
from vlah.runtime.local_assembly import LocalAssembly
from vlah.runtime.stack import KIND_CONTEXT
from vlah.testing.boundary import (
    BoundaryExpectations,
    RunInSandbox,
    parse_report,
    probe_argv,
)
from vlah.testing.conformance import (
    ExecutorCheckInputs,
    RecordingHeartbeatSink,
    check_executor,
)

pytestmark = [
    pytest.mark.infra,
    pytest.mark.executor,
    pytest.mark.kubernetes,
    pytest.mark.timeout(1500),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "examples" / "prompt_optimization" / "setup.py"
WORKER = ActorId("worker-01")
WORKERS = (ActorId("worker-01"), ActorId("worker-02"))
HERD_RUN_ID = "r-kind-herd"
HERD_TIMEOUT_SECONDS = 900.0
POD_TIMEOUT_SECONDS = 180.0
FAKE_SCORE_JUDGE = '''\
"""A judge for the scripted worker's candidates: the scores in fake_score.json."""
import json
import sys
from pathlib import Path

score_file = Path(sys.argv[1]) / "fake_score.json"
if score_file.is_file():
    per_seed = json.loads(score_file.read_text())["per_seed"]
else:
    per_seed = [0.0] * 5
print(json.dumps({"score": sum(per_seed) / len(per_seed), "per_seed": per_seed}))
'''


class RealTimer:
    def wait_seconds(self, seconds: float) -> None:
        time.sleep(seconds)


class NoClaims:
    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        return ()


def cluster_unavailable_reason() -> str | None:
    if shutil.which("kubectl") is None:
        return "kubectl is not installed"
    else:
        answer = subprocess.run(
            ["kubectl", "--context", KIND_CONTEXT, "cluster-info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return (
            None
            if answer.returncode == 0
            else f"no kind cluster {KIND_CONTEXT}: run vlah stack --kind up"
        )


@pytest.fixture
def home_path(tmp_path: Path) -> Iterator[Path]:
    """A scratch directory under the home: pytest's tmp_path is not on the node's
    mount, so a pod would see an empty directory in its place."""
    root = Path.home() / ".vlah-kind-tests" / tmp_path.name
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def kube() -> KubectlCli:
    reason = cluster_unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    else:
        return KubectlCli(context=KIND_CONTEXT)


def test_the_kubernetes_executor_meets_the_port_and_holds_the_boundary(
    home_path: Path, worker_image: str, kube: KubectlCli
) -> None:
    tmp_path = home_path
    run_id = "r-kind-check"
    blackboard = open_blackboard(tmp_path / "runs", run_id, "blackboard")
    blackboard.write(ActorId("human"), {"spec.md": b"the spec"}, "spec")
    target = make_target_repository(tmp_path / "target")
    params = Params(heartbeat_interval_seconds=2.0)
    claims: ClaimLedger = NoClaims()
    executor = KubernetesExecutor(
        run_id=run_id,
        image=worker_image,
        sandbox_root=tmp_path / "sandboxes",
        log_root=tmp_path / "logs",
        blackboard=blackboard,
        objects=MemoryObjectStore(),
        claims=claims,
        params=params,
        clock=SystemClock(),
        kube=kube,
        resources=PodResources(cpus=0.5, memory="512m"),
        grace_seconds=2.0,
        poll_seconds=0.5,
    )

    def spec(program: str) -> AgentSpec:
        return AgentSpec(
            agent=WORKER,
            command=(IMAGE_PYTHON, "-c", program),
            target_repository=str(target),
            target_ref="main",
            blackboard_clone_url=blackboard.clone_url(),
        )

    try:
        check_executor(
            executor,
            ExecutorCheckInputs(
                long_running_spec=spec("import time; time.sleep(600)"),
                quick_spec=spec("pass"),
                task=TASK,
                start_pack=StartPack(files={"CONTEXT.md": "# Task\n"}),
                heartbeat_interval_seconds=params.heartbeat_interval_seconds,
            ),
            RealTimer(),
        )
        handle = executor.start(
            spec("import time; time.sleep(600)"),
            TASK.model_copy(update={"id": "t-00002"}),
            StartPack(files={"CONTEXT.md": "# Task\n"}),
            RecordingHeartbeatSink(),
        )
        job_name = executor.job_name(handle)
        pod_name = wait_for_pod(kube, executor.namespace, job_name)

        def run_in_pod(argv: tuple[str, ...]) -> tuple[int, str]:
            completed = kube.kubectl(
                "exec", "-n", executor.namespace, pod_name, "--", *argv, check=False
            )
            return completed.returncode, completed.stdout + completed.stderr

        report = probe_pod(run_in_pod)
        assert report["herd_yaml_found"] == [] and report["private_found"] == []
        assert report["hidden_readable"] == []
        assert report["push_token_present"] is False
        assert str(report["raw_public_address"]).startswith("blocked:"), report
        assert str(report["blocked_via_proxy"]).startswith("blocked:"), report
        token_check = run_in_pod(
            ("sh", "-c", "test ! -e /var/run/secrets/kubernetes.io")
        )
        assert (
            token_check[0] == 0
        ), "a service account token is mounted in the worker pod"
        executor.kill(handle)
    finally:
        executor.close()
    assert kube.jobs_with_label(executor.namespace, run_label(run_id)) == ()


def wait_for_pod(kube: KubectlCli, namespace: str, job_name: str) -> str:
    deadline = time.monotonic() + POD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = kube.pod_state(namespace, job_name)
        if state.pod_name is not None:
            ready = kube.kubectl(
                "get",
                "pod",
                "-n",
                namespace,
                state.pod_name,
                "-o",
                "jsonpath={.status.phase}",
                check=False,
            )
            if ready.stdout.strip() == "Running":
                return state.pod_name
            else:
                pass
        else:
            pass
        time.sleep(1.0)
    raise AssertionError(f"the pod of {job_name} never ran")


def probe_pod(run_in_pod: RunInSandbox) -> dict[str, JsonValue]:
    """The boundary probe from inside the pod: the hidden paths are this machine's."""
    exit_code, output = run_in_pod(
        probe_argv(
            BoundaryExpectations(
                python_executable=IMAGE_PYTHON,
                tool_url="http://egress-proxy:3128/",
                search_roots=("/",),
                hidden_paths=("/Users",),
                allowed_variables=(),
            )
        )
    )
    assert exit_code == 0, output[-2000:]
    return parse_report(output)


def kind_problem(tmp_path: Path, image: str) -> Path:
    root = tmp_path / "prompt-opt"
    subprocess.run([sys.executable, str(SETUP), str(root)], check=True)
    (root / "private" / "judge.py").write_text(FAKE_SCORE_JUDGE, encoding="utf-8")
    herd_path = root / "herd.yaml"
    config: object = yaml.safe_load(herd_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    config["executor"] = {
        "kind": "kubernetes",
        "settings": {
            "target_repository": str(root / "target"),
            "target_ref": "main",
            "image": image,
            "resources": {"cpus": 1, "memory": "1g"},
        },
    }
    config["harness"] = {"kind": "scripted", "settings": {"script": "submit_one"}}
    herd_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def claims_by(blackboard: Blackboard, agent: ActorId) -> list[Claim]:
    return [
        parse_claim(blackboard.read(path, None).decode("utf-8"))
        for path in blackboard.list_paths(f"claims/{agent}/", None)
    ]


def follow_ups_done(blackboard: Blackboard) -> bool:
    return all(
        any(claim.type == "dead_end" and "stub" not in claim.tags for claim in claims)
        for claims in (claims_by(blackboard, agent) for agent in WORKERS)
    )


def merged_events(blackboard: Blackboard) -> list[Event]:
    return [
        event_from_json(blackboard.read(pointer, None).decode("utf-8"))
        for pointer in [
            blackboard.read(f"events/_seq/{sequence}", None).decode("utf-8")
            for sequence in range(1, blackboard.head_sequence() + 1)
        ]
    ]


def test_two_scripted_workers_run_the_herd_on_kind(
    home_path: Path, worker_image: str, kube: KubectlCli
) -> None:
    root = kind_problem(home_path, worker_image)
    problem = load_problem(root)
    assembly = LocalAssembly(kube=kube, executor_poll_seconds=0.5, waiter_seconds=1.0)
    run = build_herd(
        problem,
        "S2",
        assembly,
        HERD_RUN_ID,
        root=root / ".herd" / "runs",
        worker_count=2,
        duration_seconds=HERD_TIMEOUT_SECONDS,
        clock=SystemClock(),
        random=Random(0),
        poll_seconds=1.0,
    ).start()
    blackboard = run.components.blackboard
    try:
        deadline = time.monotonic() + HERD_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not follow_ups_done(blackboard):
            time.sleep(2.0)
        run.stop()
    finally:
        assembly.close()
    assert follow_ups_done(blackboard), "both workers answered their results"
    assert len(merged_events(blackboard)) == blackboard.head_sequence()
    assert not kube.namespace_exists(f"vlah-{HERD_RUN_ID}"), "the namespace is gone"
