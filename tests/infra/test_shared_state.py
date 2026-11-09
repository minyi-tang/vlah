"""Shared state on the stack (#130): the Redis store meets the port on a real
server, a second driver is refused, and a run crashed and resumed on the stack
assigns no sequence number twice and loses no result's follow-up (#164)."""

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
from vlah.core.queue.queue import (
    DONE_KEY_PREFIX,
    FOLLOW_UP_KEY_PREFIX,
    QUEUE_KEY,
    TASK_KEY_PREFIX,
    read_leases,
)
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import FakeClock, SystemClock
from vlah.core.types.events import ResultReady, event_from_json
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
FOLLOW_UP_CLAIM_TIMEOUT_SECONDS = 400.0
RESUME_TIMEOUT_SECONDS = 600.0
# The driver runs in slices this long between two looks at the store. A session
# takes far longer to start, so no claim passes between two looks unseen.
DRIVER_RUN_SLICE_SECONDS = 0.05
# A command the driver sent just before it was stopped has landed after this long.
DRIVER_SETTLE_SECONDS = 0.05
OUTPUT_EXCERPT_CHARACTERS = 4000
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


def vlah(output_path: Path, *arguments: str) -> subprocess.Popen[bytes]:
    """Start vlah with everything it prints in a file: a pipe the test does not
    read fills up and blocks a driver that prints for minutes."""
    with output_path.open("wb") as output:
        return subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "from vlah.cli import main; raise SystemExit(main())",
                *arguments,
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=REPO_ROOT,
        )


def printed_by(output_path: Path) -> str:
    return output_path.read_text(encoding="utf-8", errors="replace")


def excerpt(output_path: Path) -> str:
    """The start and the end of the output: a resumed driver says what it took
    back from the earlier process in its first lines."""
    printed = printed_by(output_path)
    if len(printed) <= 2 * OUTPUT_EXCERPT_CHARACTERS:
        return printed
    else:
        return (
            f"{printed[:OUTPUT_EXCERPT_CHARACTERS]}\n[...]\n"
            f"{printed[-OUTPUT_EXCERPT_CHARACTERS:]}"
        )


def git_on_blackboard(root: Path, *arguments: str) -> str:
    """Read the run's repository with git itself: a store object of this process
    would cache the tree it first saw, and opening one would create the repository
    before the run does. Empty when git finds nothing, or no repository yet."""
    repository = root / ".herd" / "runs" / RUN_ID / "blackboard.git"
    return subprocess.run(
        ["git", "--git-dir", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def blackboard_texts(root: Path, prefix: str, word: str) -> tuple[str, ...]:
    """The text of every file under prefix that contains word."""
    matches = git_on_blackboard(
        root,
        "grep",
        "--files-with-matches",
        "--fixed-strings",
        word,
        "main",
        "--",
        prefix,
    )
    return tuple(
        git_on_blackboard(root, "show", revision_path)
        for revision_path in matches.split()
    )


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


def follow_ups_done(root: Path) -> bool:
    """Each worker has a result, and every result has an answer that cites it: a
    dead end that is not an executor stub, written by any worker (D28, D55).

    The addressee holds no lease between two tasks, so its peer may take the
    follow-up; and a follow-up whose session was killed runs again, so a result may
    be answered twice."""
    results = [
        event.data
        for event in map(
            event_from_json, blackboard_texts(root, "events/", "result_ready")
        )
        if event.type == "result_ready" and isinstance(event.data, ResultReady)
    ]
    answered = {
        claim.evidence[0].claim
        for claim in map(parse_claim, blackboard_texts(root, "claims/", "dead_end"))
        if claim.type == "dead_end"
        and claim.author in WORKERS
        and "stub" not in claim.tags
    }
    return {result.submitter for result in results} == set(WORKERS) and all(
        result.claim_id in answered for result in results
    )


def unheld_follow_ups(store: RedisKeyValueStore) -> tuple[str, ...]:
    """Result follow-ups that are in no queue, under no lease and not done."""
    queued = {task_id for task_id, _ in store.sorted_range(QUEUE_KEY)}
    leased = {lease.task_id for lease in read_leases(store)}
    follow_ups = [
        (store.get(follow_up_key) or b"").decode()
        for follow_up_key in store.scan(FOLLOW_UP_KEY_PREFIX)
    ]
    return tuple(
        task_id
        for task_id in follow_ups
        if task_id not in queued | leased
        and store.get(f"{TASK_KEY_PREFIX}{task_id}") is not None
        and store.get(f"{DONE_KEY_PREFIX}{task_id}") is None
    )


def stop_with_a_follow_up_unheld(
    driver: subprocess.Popen[bytes], store: RedisKeyValueStore
) -> tuple[str, ...]:
    """Stop the driver, with SIGSTOP, while a result's follow-up is in no queue and
    under no lease, and return those follow-ups.

    That is so once a worker has taken the follow-up off the queue and until its
    session has a lease, and for a moment between the queue writing the task and
    listing it. The store is read only while the driver is stopped, so nothing
    changes under the read. Empty if the driver ended or no such moment came."""
    deadline = time.monotonic() + FOLLOW_UP_CLAIM_TIMEOUT_SECONDS
    while time.monotonic() < deadline and driver.poll() is None:
        driver.send_signal(signal.SIGSTOP)
        time.sleep(DRIVER_SETTLE_SECONDS)
        unheld = unheld_follow_ups(store)
        if unheld:
            return unheld
        else:
            driver.send_signal(signal.SIGCONT)
            time.sleep(DRIVER_RUN_SLICE_SECONDS)
    return ()


def resume_once_the_lock_expires(
    root: Path, output_path: Path
) -> subprocess.Popen[bytes]:
    """A crashed driver's lock outlives it by its TTL; a resume before that is refused
    and says so, which the test treats as "try again" (D56, #130)."""
    deadline = time.monotonic() + LOCK_EXPIRY_TIMEOUT_SECONDS
    while True:
        resumed = vlah(
            output_path, "run", str(root), "--stack", "docker", "--resume", RUN_ID
        )
        try:
            resumed.wait(timeout=LOCK_REFUSAL_SECONDS)
        except subprocess.TimeoutExpired:
            return resumed
        if "driven by" in printed_by(output_path) and time.monotonic() < deadline:
            time.sleep(LOCK_RETRY_SECONDS)
        else:
            raise AssertionError(f"the resume ended at once:\n{excerpt(output_path)}")


def test_a_run_killed_mid_way_resumes_on_the_stack_without_repeating_work(
    tmp_path: Path, worker_image: str, redis_up: redis.Redis
) -> None:
    """SIGKILL the driver at the worst moment for a result's follow-up: it is in no
    queue and under no lease, as when a worker has claimed it and its session is
    still starting (#164). The resume takes back the crashed run's containers,
    leases and that follow-up, and the event sequence stays contiguous."""
    root = stack_problem(tmp_path, worker_image)
    stack_down(run_command, sys.stdout, run_id=RUN_ID, remove_volumes=False)
    # An earlier run of this test on the same server left its queue under this run id.
    for stale_key in redis_up.scan_iter(f"{RUN_ID}:*"):
        redis_up.delete(stale_key)
    store = RedisKeyValueStore(client=redis_up, clock=SystemClock(), namespace=RUN_ID)
    first_output = tmp_path / "first.txt"
    first = vlah(
        first_output,
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
        unheld = stop_with_a_follow_up_unheld(first, store)
        assert unheld, (
            "no follow-up was ever in no queue and under no lease; vlah run "
            f"printed:\n{excerpt(first_output)}"
        )
        # A driver that is stopped, not dead, still holds the run.
        held_output = tmp_path / "held.txt"
        held = vlah(
            held_output, "run", str(root), "--stack", "docker", "--resume", RUN_ID
        )
        held.wait(timeout=120)
        assert held.returncode != 0 and "driven by" in printed_by(
            held_output
        ), printed_by(held_output)
    finally:
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=30)
    head_before = len(
        git_on_blackboard(
            root, "ls-tree", "--name-only", "main", "events/_seq/"
        ).split()
    )
    assert head_before > 0
    resumed_output = tmp_path / "resumed.txt"
    resumed = resume_once_the_lock_expires(root, resumed_output)
    try:
        finished = wait_until(RESUME_TIMEOUT_SECONDS, lambda: follow_ups_done(root))
        assert finished, (
            f"the resumed run did not answer both results. At the kill {unheld} "
            f"were in no queue and under no lease; now {unheld_follow_ups(store)} "
            f"are. The resumed run printed:\n{excerpt(resumed_output)}"
        )
    finally:
        stop = vlah(tmp_path / "stop.txt", "stop", str(root), "--run-id", RUN_ID)
        stop.wait(timeout=60)
        resumed.wait(timeout=600)
    output = printed_by(resumed_output)
    assert resumed.returncode == 0, excerpt(resumed_output)
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
