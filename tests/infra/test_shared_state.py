"""Shared state on the stack (#130): the Redis store meets the port on a real
server, a second driver is refused, and a run crashed and resumed on the stack
assigns no sequence number twice and runs no task twice."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest
import redis
import yaml

from vlah.adapters.store.redis.store import RedisKeyValueStore
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.runtime.driver import DriverLock, RunHeldError
from vlah.runtime.stack import run_command, stack_down, stack_up
from vlah.runtime.stack_assembly import STACK_REDIS_URL
from vlah.testing.conformance import check_key_value_store

pytestmark = [
    pytest.mark.infra,
    pytest.mark.runtime,
    pytest.mark.queue,
    pytest.mark.docker,
    pytest.mark.timeout(1500),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "examples" / "prompt_optimization" / "setup.py"
RUN_ID = "r-stack-resume"
WORKERS = (ActorId("worker-01"), ActorId("worker-02"))
THREAD_COUNT = 8
INCREMENTS_PER_THREAD = 50
FIRST_CLAIM_TIMEOUT_SECONDS = 400.0
RESUME_TIMEOUT_SECONDS = 600.0
# A refused resume exits within moments; a run that gets past the lock keeps going.
LOCK_REFUSAL_SECONDS = 20.0
LOCK_RETRY_SECONDS = 10.0
# The CLI's lock TTL is 120 s; the retries cover it with room.
LOCK_EXPIRY_TIMEOUT_SECONDS = 240.0
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


@pytest.fixture(scope="module")
def redis_up() -> Iterator[redis.Redis]:
    assert stack_up(run_command, sys.stdout) == 0, "vlah stack up failed"
    client = redis.Redis.from_url(STACK_REDIS_URL)
    yield client
    client.close()


class ClockTimer:
    """The Redis store decides expiry by the injected clock, so waiting is advancing it."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def wait_seconds(self, seconds: float) -> None:
        self.clock.advance(seconds)


def test_the_redis_store_meets_the_port_on_a_real_server(redis_up: redis.Redis) -> None:
    clock = FakeClock(current_seconds=1000.0)
    namespace = f"conformance-{os.getpid()}-{time.time_ns()}"
    store = RedisKeyValueStore(client=redis_up, clock=clock, namespace=namespace)
    check_key_value_store(store, ClockTimer(clock))
    counter = "contended/count"
    winners: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(INCREMENTS_PER_THREAD):
            store.increment(counter, 1)
        if store.compare_and_set("contended/once", None, b"mine", None):
            with lock:
                winners.append(1)
        else:
            pass

    threads = [threading.Thread(target=worker) for _ in range(THREAD_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.increment(counter, 0) == THREAD_COUNT * INCREMENTS_PER_THREAD
    assert len(winners) == 1, "compare-and-set lets exactly one caller through"
    for key in redis_up.scan_iter(f"{namespace}:*"):
        redis_up.delete(key)


def test_a_second_driver_on_the_stack_is_refused(redis_up: redis.Redis) -> None:
    clock = FakeClock(current_seconds=1000.0)
    namespace = f"driver-{os.getpid()}-{time.time_ns()}"
    store = RedisKeyValueStore(client=redis_up, clock=clock, namespace=namespace)
    first = DriverLock(store=store, run_id="r-x", clock=clock, ttl_seconds=60.0)
    first.acquire()
    second = DriverLock(store=store, run_id="r-x", clock=clock, ttl_seconds=60.0)
    with pytest.raises(RunHeldError, match="pid"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def stack_problem(tmp_path: Path, image: str) -> Path:
    root = tmp_path / "prompt-opt"
    subprocess.run([sys.executable, str(SETUP), str(root)], check=True)
    (root / "private" / "judge.py").write_text(FAKE_SCORE_JUDGE, encoding="utf-8")
    herd_path = root / "herd.yaml"
    config: object = yaml.safe_load(herd_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    config["executor"]["settings"]["image"] = image
    config["executor"]["settings"]["resources"] = {"cpus": 1, "memory": "1g"}
    config["harness"] = {"kind": "scripted", "settings": {"script": "submit_one"}}
    herd_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def vlah(*arguments: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "from vlah.cli import main; raise SystemExit(main())",
            *arguments,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=REPO_ROOT,
    )


def blackboard_paths(root: Path, prefix: str) -> tuple[str, ...]:
    """Read the run's repository with git itself: a store object of this process
    would cache the tree it first saw, and opening one would create the repository
    before the run does."""
    repository = root / ".herd" / "runs" / RUN_ID / "blackboard.git"
    if not repository.is_dir():
        return ()
    else:
        listing = subprocess.run(
            [
                "git",
                "--git-dir",
                str(repository),
                "ls-tree",
                "-r",
                "--name-only",
                "main",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return tuple(path for path in listing.stdout.split() if path.startswith(prefix))


class Condition(Protocol):
    def __call__(self) -> bool: ...


def wait_until(timeout_seconds: float, condition: Condition) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        else:
            time.sleep(2.0)
    return False


def claim_text(root: Path, path: str) -> str:
    repository = root / ".herd" / "runs" / RUN_ID / "blackboard.git"
    return subprocess.run(
        ["git", "--git-dir", str(repository), "show", f"main:{path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def follow_ups_done(root: Path) -> bool:
    """Each worker has answered its result with a dead end (D28)."""
    return all(
        any(
            parse_claim(claim_text(root, path)).type == "dead_end"
            for path in blackboard_paths(root, f"claims/{agent}/")
        )
        for agent in WORKERS
    )


def resume_once_the_lock_expires(root: Path) -> subprocess.Popen[str]:
    """A crashed driver's lock outlives it by its TTL; a resume before that is refused
    and says so, which the test treats as "try again" (D56, #130)."""
    deadline = time.monotonic() + LOCK_EXPIRY_TIMEOUT_SECONDS
    while True:
        resumed = vlah("run", str(root), "--stack", "docker", "--resume", RUN_ID)
        try:
            printed, _ = resumed.communicate(timeout=LOCK_REFUSAL_SECONDS)
        except subprocess.TimeoutExpired:
            return resumed
        if "driven by" in printed and time.monotonic() < deadline:
            time.sleep(LOCK_RETRY_SECONDS)
        else:
            raise AssertionError(f"the resume ended at once:\n{printed[-4000:]}")


def test_a_run_killed_mid_way_resumes_on_the_stack_without_repeating_work(
    tmp_path: Path, worker_image: str, redis_up: redis.Redis
) -> None:
    """SIGKILL the driver once a worker has written a claim; the resume reclaims the
    crashed run's containers and leases, and the event sequence stays contiguous."""
    root = stack_problem(tmp_path, worker_image)
    stack_down(run_command, sys.stdout, run_id=RUN_ID, remove_volumes=False)
    first = vlah(
        "run",
        str(root),
        "--stack",
        "docker",
        "--arm",
        "S2",
        "--workers",
        "2",
        "--hours",
        "0.5",
        "--run-id",
        RUN_ID,
    )
    try:
        started = wait_until(
            FIRST_CLAIM_TIMEOUT_SECONDS,
            lambda: any(
                blackboard_paths(root, f"claims/{agent}/") for agent in WORKERS
            ),
        )
        if not started:
            first.kill()
            printed, _ = first.communicate(timeout=60)
            raise AssertionError(
                f"no worker wrote a claim before the crash; vlah run printed:\n{printed[-4000:]}"
            )
        else:
            pass
        held = vlah("run", str(root), "--stack", "docker", "--resume", RUN_ID)
        output, _ = held.communicate(timeout=120)
        assert held.returncode != 0 and "driven by" in output, output
    finally:
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=30)
    head_before = len(blackboard_paths(root, "events/_seq/"))
    resumed = resume_once_the_lock_expires(root)
    try:
        finished = wait_until(RESUME_TIMEOUT_SECONDS, lambda: follow_ups_done(root))
        if not finished:
            resumed.kill()
            printed, _ = resumed.communicate(timeout=60)
            raise AssertionError(
                "the resumed run did not finish both workers' follow-ups; it printed:\n"
                f"{printed[-4000:]}"
            )
        else:
            pass
    finally:
        stop = vlah("stop", str(root), "--run-id", RUN_ID)
        stop.wait(timeout=60)
        output, _ = resumed.communicate(timeout=600)
    assert resumed.returncode == 0, output
    blackboard = open_blackboard(root / ".herd" / "runs", RUN_ID, "blackboard")
    head = blackboard.head_sequence()
    assert head > head_before
    for sequence in range(1, head + 1):
        blackboard.read(f"events/_seq/{sequence}", None)
    claim_paths = [
        path
        for agent in WORKERS
        for path in blackboard.list_paths(f"claims/{agent}/", None)
    ]
    assert len(claim_paths) == len(set(claim_paths))
    assert (
        "driven by" not in output
    ), "the crashed driver's lock expired or was never renewed"
