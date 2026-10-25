"""The Kubernetes executor over a fake cluster: what the Job and the namespace are
told, how a session ends, restarts and is interrupted, and what a resume sweeps (#131)."""

from pathlib import Path

import pytest
from pydantic import JsonValue

from tests.fixtures.executor import START_PACK, TASK, WORKER
from tests.fixtures.kubernetes import (
    QUICK_PROGRAM,
    RUN_ID,
    SLEEP_PROGRAM,
    WORKER_IMAGE,
    FakeKubernetes,
    running_kubernetes_executor,
)
from vlah.adapters.executor.kubernetes.client import (
    JobSpec,
    job_manifest,
    kubernetes_name,
    parse_pod_state,
)
from vlah.adapters.executor.kubernetes.egress import (
    AGENT_LABEL,
    ATTEMPT_LABEL,
    FIRST_PROXY_PORT,
    LOGS_MOUNT,
    PROXY_AGENT,
    PROXY_NAME,
    RUN_LABEL,
    TASK_LABEL,
    namespace_for,
)
from vlah.adapters.executor.kubernetes.executor import HOME_DIRECTORY, WORK_DIRECTORY
from vlah.adapters.executor.local.traces import STUB_TAG
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import EgressPolicy, HostRule

pytestmark = [pytest.mark.executor, pytest.mark.kubernetes]

POLICY = EgressPolicy(
    allow=(HostRule(host="api.example.com", ports=(443,), purpose="model_api"),)
)
INTERRUPT = Interrupt(
    id="i-1", agent=WORKER, kind="hard", rule=2, reason="rebase", written_ts=0.0
)


def pod_spec_of(manifest: dict[str, JsonValue]) -> dict[str, JsonValue]:
    spec = manifest["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    pod_spec = template["spec"]
    assert isinstance(pod_spec, dict)
    return pod_spec


def test_a_session_is_a_job_that_mounts_only_its_sandbox_and_runs_unprivileged(
    tmp_path: Path,
) -> None:
    """The guardrails of #131 read back from the manifests: no service account token,
    non-root, limits, labels, one hostPath scoped to the task, default-deny egress."""
    kube = FakeKubernetes()
    with running_kubernetes_executor(tmp_path, FakeClock(), kube=kube) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM, env={"KEEP": "1"}, egress=POLICY),
            TASK,
            START_PACK,
            harness.sink,
        )
        sandbox = harness.sandbox()
        namespace = namespace_for(RUN_ID)
        (job,) = kube.worker_jobs()
        pod_spec = pod_spec_of(job.manifest)
        assert job.spec.namespace == namespace
        assert pod_spec["restartPolicy"] == "Never"
        assert pod_spec["automountServiceAccountToken"] is False
        (volume,) = pod_spec["volumes"] if isinstance(pod_spec["volumes"], list) else []
        assert isinstance(volume, dict)
        assert volume["hostPath"] == {
            "path": str(sandbox.resolve()),
            "type": "Directory",
        }
        (container,) = (
            pod_spec["containers"] if isinstance(pod_spec["containers"], list) else []
        )
        assert isinstance(container, dict)
        assert container["image"] == WORKER_IMAGE
        assert container["command"] == ["python", "-c", SLEEP_PROGRAM]
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "runAsUser": 1001,
        }
        assert container["resources"] == {
            "limits": {"cpu": "2.0", "memory": "2Gi"},
            "requests": {"cpu": "2.0", "memory": "2Gi"},
        }
        assert container["volumeMounts"] == [
            {"name": "sandbox", "mountPath": WORK_DIRECTORY, "readOnly": False}
        ]
        assert job.spec.labels == {
            RUN_LABEL: RUN_ID,
            AGENT_LABEL: WORKER,
            TASK_LABEL: TASK.id,
            ATTEMPT_LABEL: "1",
        }
        environment = job.spec.environment
        assert environment["KEEP"] == "1"
        assert environment["HOME"] == HOME_DIRECTORY
        assert environment["VLAH_SANDBOX"] == WORK_DIRECTORY
        assert environment["VLAH_TRANSCRIPT"].startswith(f"{WORK_DIRECTORY}/.vlah/")
        assert "PATH" not in environment, "nothing of the host is inherited"
        proxy_url = f"http://{PROXY_NAME}:{FIRST_PROXY_PORT}"
        assert environment["HTTP_PROXY"] == environment["HTTPS_PROXY"] == proxy_url
        assert environment["NO_PROXY"] == ""
        assert (sandbox / ".home").is_dir()
        assert harness.executor.transcript_path(handle) == sandbox / ".vlah" / (
            f"{handle.handle_id}-1.transcript.jsonl"
        )

        assert ("Namespace", "", namespace) in kube.manifests
        policy = kube.manifests[("NetworkPolicy", namespace, "deny-workers-egress")]
        policy_spec = policy["spec"]
        assert isinstance(policy_spec, dict)
        assert policy_spec["policyTypes"] == ["Egress"]
        proxy = kube.proxy_deployment()
        proxy_pod = pod_spec_of(proxy)
        assert proxy_pod["automountServiceAccountToken"] is False
        (logs_volume,) = (
            proxy_pod["volumes"] if isinstance(proxy_pod["volumes"], list) else []
        )
        assert isinstance(logs_volume, dict)
        assert logs_volume["hostPath"] == {
            "path": str(harness.log_root),
            "type": "DirectoryOrCreate",
        }
        assert ("Service", namespace, PROXY_NAME) in kube.manifests
        request = harness.listener_request()
        assert (request.port, request.generation, request.policy) == (
            FIRST_PROXY_PORT,
            1,
            POLICY,
        )
        assert harness.sink.beats == [(WORKER, (), 0.0)], "the first beat at start"


def test_unenforced_egress_leaves_the_deny_policy_out(tmp_path: Path) -> None:
    kube = FakeKubernetes()
    with running_kubernetes_executor(
        tmp_path, FakeClock(), kube=kube, allow_unenforced=True
    ) as harness:
        harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        assert not any(key[0] == "NetworkPolicy" for key in kube.manifests)


def test_an_exit_without_a_claim_restarts_once_then_leaves_a_stub(
    tmp_path: Path,
) -> None:
    kube = FakeKubernetes()
    clock = FakeClock()
    with running_kubernetes_executor(tmp_path, clock, kube=kube) as harness:
        handle = harness.executor.start(
            harness.spec(QUICK_PROGRAM), TASK, START_PACK, harness.sink
        )
        first = harness.executor.job_name(handle)
        kube.exit(first, 0)
        harness.executor.tick()
        assert harness.executor.is_alive(handle), "restarted once with a hint"
        second = harness.executor.job_name(handle)
        assert second != first and second.endswith("-2")
        kube.exit(second, 0)
        harness.executor.tick()
        assert not harness.executor.is_alive(handle)
        exit_info = harness.executor.wait(handle, 1.0)
        assert exit_info is not None and exit_info.return_code == 0
        claims = [
            parse_claim(harness.blackboard.read(path, None).decode())
            for path in harness.blackboard.list_paths(f"claims/{WORKER}/", None)
        ]
        assert [STUB_TAG in claim.tags for claim in claims] == [True]
        assert harness.blackboard.list_paths(f"traces/{WORKER}/", None)
        assert all(job.deleted_with_grace == 0.0 for job in kube.worker_jobs())


def test_an_interrupt_deletes_the_job_and_starts_another_on_the_sandbox(
    tmp_path: Path,
) -> None:
    kube = FakeKubernetes()
    with running_kubernetes_executor(tmp_path, FakeClock(), kube=kube) as harness:
        handle = harness.executor.start(
            harness.spec(SLEEP_PROGRAM), TASK, START_PACK, harness.sink
        )
        first = harness.executor.job_name(handle)
        restarted = harness.executor.interrupt(handle, INTERRUPT)
        assert restarted.handle_id != handle.handle_id
        assert kube.jobs[(namespace_for(RUN_ID), first)].deleted_with_grace == 0.5
        assert harness.executor.sandbox_path(WORKER, TASK.id) == harness.sandbox()
        context = (harness.sandbox() / "CONTEXT.md").read_text()
        assert INTERRUPT.reason in context
        kill_exit = harness.executor.wait(handle, 1.0)
        assert kill_exit is not None and kill_exit.was_killed


def test_a_crashed_runs_namespace_is_deleted_before_the_executor_starts(
    tmp_path: Path,
) -> None:
    kube = FakeKubernetes()
    kube.add_orphan_namespace(namespace_for(RUN_ID))
    kube.add_orphan_namespace(namespace_for("run-2"))
    with running_kubernetes_executor(tmp_path, FakeClock(), kube=kube):
        assert kube.deleted_namespaces == [namespace_for(RUN_ID)]
        assert kube.namespace_exists(namespace_for("run-2")), "another run stays"
    assert kube.deleted_namespaces[-1] == namespace_for(RUN_ID), "close deletes it too"


def test_names_and_pod_states_read_as_the_cluster_writes_them() -> None:
    assert kubernetes_name("R-20261025-1200-S2") == "r-20261025-1200-s2"
    assert kubernetes_name("w-kube-00001-1") == "w-kube-00001-1"
    assert len(kubernetes_name("x" * 100)) == 63
    pending = parse_pod_state({"items": []})
    assert pending.is_running and pending.pod_name is None
    terminated = parse_pod_state(
        {
            "items": [
                {
                    "metadata": {"name": "w-1-abc", "creationTimestamp": "2026"},
                    "status": {
                        "phase": "Failed",
                        "containerStatuses": [
                            {
                                "state": {
                                    "terminated": {
                                        "exitCode": 137,
                                        "reason": "OOMKilled",
                                    }
                                }
                            }
                        ],
                    },
                }
            ]
        }
    )
    assert (terminated.is_running, terminated.exit_code, terminated.oom_killed) == (
        False,
        137,
        True,
    )
    manifest = job_manifest(
        JobSpec(
            name="w-1",
            namespace="vlah-r",
            image=WORKER_IMAGE,
            argv=("python", "-c", "pass"),
            environment={},
            labels={},
            volumes=(),
            working_directory=WORK_DIRECTORY,
            cpus=None,
            memory="512m",
        )
    )
    (container,) = pod_spec_of(manifest)["containers"]  # type: ignore[misc]
    assert isinstance(container, dict)
    assert container["resources"] == {
        "limits": {"memory": "512Mi"},
        "requests": {"memory": "512Mi"},
    }
    assert LOGS_MOUNT == "/logs" and PROXY_AGENT == "egress-proxy"
