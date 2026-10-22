"""Reproduce the H11 spike: checkpoint a Claude Code worker on machine A, then fork it on machine B.

Docker containers with separate volumes stand in for the two machines, and a scripted mock stands in
for the model API, so a run needs Docker and network access to npm and PyPI but no API key.
One command: python3 spikes/checkpoint/run.py
"""

import argparse
import json
import secrets
import statistics
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SPIKE_DIRECTORY = Path(__file__).resolve().parent
IMAGE_NAME = "vlah-h11-worker"
NETWORK_NAME = "h11-net"
MOCK_CONTAINER = "h11-mock"
SOURCE_CONTAINER = "h11-machine-a"
TARGET_CONTAINER = "h11-machine-b"
REMOTE_VOLUME = "h11-remote"
SOURCE_VOLUME = "h11-machine-a-volume"
TARGET_VOLUME = "h11-machine-b-volume"
MOCK_PORT = 8080
MOCK_LOG_PATH = "/tmp/mock.jsonl"
WORKER_BRANCH = "worker/w1"
TRANSCRIPT_DIRECTORY = "/home/node/.claude/projects/-work-repo"
APPROACHES = (
    "tar_zstd",
    "git_bundle",
    "overlay_upper",
    "command_replay",
    "tar_without_venv",
)
PASS_SECONDS = 120  # H11 pass bar, v2.2 section 11
PASS_BYTES = 1_000_000_000
BYTES_PER_MEGABYTE = 1_000_000
CACHE_DIRECTORY_NAME = "__pycache__"
EXAMPLE_PATH_COUNT = 5
FIRST_PROMPT = (
    "Goal 1: study the backfill sweep so far. "
    "[[bash:cat .h11/nonce.txt && git log --oneline -3 && seq 1 80000]]"
)
FORK_PROMPT = (
    "Goal 2, forked from a checkpoint: try a 15-minute backfill window. "
    "[[bash:date +%s.%N > /tmp/h11_first_tool_call && echo {recalled_nonce} > /tmp/h11_recalled_nonce"
    " && git status --short | head -n 5]]"
)
REMOTE_SETUP_SCRIPT = """
set -euo pipefail
git init -q --bare -b main /remote/target.git
git -C /remote/target.git fetch -q /in/target.bundle HEAD:refs/heads/main
git clone -q /remote/target.git /tmp/base/repo
git -C /tmp/base/repo rev-parse HEAD
chown -R 1000:1000 /tmp/base
tar -C /tmp/base -cf /remote/base.tar repo
"""


@dataclass(kw_only=True)
class Fidelity:
    matching_paths: int
    missing_paths: int
    extra_paths: int
    differing_paths: int
    differing_bytes: int
    differing_paths_outside_caches: int
    git_state_matches: bool
    git_fields_differing: list[str]
    example_paths: list[str]


@dataclass(kw_only=True)
class ForkMeasurement:
    approach: str
    repetition: int
    checkpoint_seconds: float
    transfer_seconds: float
    restore_seconds: float
    resume_to_first_tool_call_seconds: float
    resume_to_first_model_request_seconds: float
    total_seconds: float
    sandbox_checkpoint_bytes: int
    conversation_checkpoint_bytes: int
    fidelity: Fidelity
    recalled_nonce_matches: bool
    history_reached_model: bool
    prompt_prefix_break: str
    spilled_tool_results_on_target: int


def docker(arguments: list[str], input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["docker", *arguments], input=input_text, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(arguments[:4])} failed:\n{completed.stderr[-3000:]}"
        )
    else:
        return completed.stdout


def machine_step(
    container: str, step: str, approach: str, environment: dict[str, str]
) -> dict[str, str]:
    """Run one machine.sh step and return the key=value lines it printed."""
    environment_flags = [
        flag
        for name, value in environment.items()
        for flag in ("-e", f"{name}={value}")
    ]
    output = docker(
        [
            "exec",
            *environment_flags,
            container,
            "bash",
            "/opt/h11/machine.sh",
            step,
            approach,
        ]
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def checkpoint_file_sizes(step_output: dict[str, str]) -> dict[str, int]:
    return {
        name.removeprefix("file:"): int(size)
        for name, size in step_output.items()
        if name.startswith("file:")
    }


def read_manifest(
    container: str,
) -> dict[str, dict[str, list[str | int]] | dict[str, str]]:
    return json.loads(
        docker(
            [
                "exec",
                "-u",
                "node",
                container,
                "python3",
                "/opt/h11/manifest.py",
                "/work/repo",
            ]
        )
    )


def compare_manifests(source: dict, target: dict) -> Fidelity:
    source_tree: dict[str, list[str | int]] = source["tree"]
    target_tree: dict[str, list[str | int]] = target["tree"]
    missing = sorted(set(source_tree) - set(target_tree))
    extra = sorted(set(target_tree) - set(source_tree))
    differing = sorted(
        path
        for path in set(source_tree) & set(target_tree)
        if source_tree[path] != target_tree[path]
    )
    changed_paths = missing + extra + differing
    outside_caches = [
        path for path in changed_paths if CACHE_DIRECTORY_NAME not in path.split("/")
    ]
    differing_bytes = sum(
        int(source_tree.get(path, target_tree.get(path, ["", 0, 0]))[2])
        for path in changed_paths
    )
    return Fidelity(
        matching_paths=len(source_tree) - len(missing) - len(differing),
        missing_paths=len(missing),
        extra_paths=len(extra),
        differing_paths=len(differing),
        differing_bytes=differing_bytes,
        differing_paths_outside_caches=len(outside_caches),
        git_state_matches=source["git"] == target["git"],
        git_fields_differing=sorted(
            name for name in source["git"] if source["git"][name] != target["git"][name]
        ),
        example_paths=(outside_caches or changed_paths)[:EXAMPLE_PATH_COUNT],
    )


def read_mock_log() -> list[dict]:
    return [
        json.loads(line)
        for line in docker(["exec", MOCK_CONTAINER, "cat", MOCK_LOG_PATH]).splitlines()
    ]


def normalized(value: object) -> object:
    """A request as the prompt cache sees it: no cache breakpoints, and string content as one text block."""
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str) and "role" in value:
            value = {**value, "content": [{"type": "text", "text": content}]}
        else:
            pass
        return {
            name: normalized(inner)
            for name, inner in value.items()
            if name != "cache_control"
        }
    elif isinstance(value, list):
        return [normalized(inner) for inner in value]
    else:
        return value


def prompt_prefix_break(source_request: dict, fork_request: dict) -> str:
    """Where the fork's first request stops repeating the source's last request; empty if it never does.

    A prompt cache can serve the fork only from the part before that point.
    """
    source = normalized(source_request)
    fork = normalized(fork_request)
    assert isinstance(source, dict) and isinstance(fork, dict)
    if source.get("tools") != fork.get("tools"):
        return "tools"
    elif source.get("system") != fork.get("system"):
        return "system"
    else:
        for index, message in enumerate(source["messages"]):
            if index >= len(fork["messages"]) or fork["messages"][index] != message:
                return f"message {index} of {len(source['messages'])}"
            else:
                pass
        return ""


def start_machines(repository_root: Path) -> str:
    """Build the image, start the mock and both machines, and return the base commit of the sandbox."""
    docker(["build", "-q", "-t", IMAGE_NAME, str(SPIKE_DIRECTORY)])
    remove_machines()
    docker(["network", "create", NETWORK_NAME])
    for volume in (REMOTE_VOLUME, SOURCE_VOLUME, TARGET_VOLUME):
        docker(["volume", "create", volume])
    with tempfile.TemporaryDirectory() as bundle_directory:
        bundle_path = f"{bundle_directory}/target.bundle"
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "bundle",
                "create",
                bundle_path,
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        base_commit = docker(
            [
                "run",
                "--rm",
                "-v",
                f"{REMOTE_VOLUME}:/remote",
                "-v",
                f"{bundle_directory}:/in:ro",
                IMAGE_NAME,
            ]
            + ["bash", "-c", REMOTE_SETUP_SCRIPT]
        ).strip()
    mock_command = [
        "python3",
        "/opt/h11/mock_anthropic.py",
        "--port",
        str(MOCK_PORT),
        "--log",
        MOCK_LOG_PATH,
    ]
    docker(
        [
            "run",
            "-d",
            "--name",
            MOCK_CONTAINER,
            "--network",
            NETWORK_NAME,
            IMAGE_NAME,
            *mock_command,
        ]
    )
    for container, volume in (
        (SOURCE_CONTAINER, SOURCE_VOLUME),
        (TARGET_CONTAINER, TARGET_VOLUME),
    ):
        docker(
            [
                "run",
                "-d",
                "--name",
                container,
                "--hostname",
                container,
                "--network",
                NETWORK_NAME,
                "--privileged",
            ]
            + ["-v", f"{volume}:/vol", "-v", f"{REMOTE_VOLUME}:/remote:ro"]
            + [
                "-e",
                f"ANTHROPIC_BASE_URL=http://{MOCK_CONTAINER}:{MOCK_PORT}",
                "-e",
                "ANTHROPIC_API_KEY=sk-ant-mock",
            ]
            + [IMAGE_NAME, "sleep", "infinity"]
        )
    machine_step(SOURCE_CONTAINER, "setup-source", "", {})
    machine_step(TARGET_CONTAINER, "setup-target", "", {})
    docker(
        [
            "exec",
            "-d",
            SOURCE_CONTAINER,
            "python3",
            "-m",
            "http.server",
            "8000",
            "--directory",
            "/vol/ckpt",
        ]
    )
    return base_commit


def remove_machines() -> None:
    for container in (MOCK_CONTAINER, SOURCE_CONTAINER, TARGET_CONTAINER):
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    for volume in (REMOTE_VOLUME, SOURCE_VOLUME, TARGET_VOLUME):
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)
    subprocess.run(["docker", "network", "rm", NETWORK_NAME], capture_output=True)


def fetch_to_target(
    approach: str, file_sizes: dict[str, int], rate_bytes_per_second: int
) -> dict[str, str]:
    environment = {
        "CHECKPOINT_FILES": " ".join(file_sizes),
        "SOURCE_HOST": SOURCE_CONTAINER,
        "RATE_BYTES_PER_SECOND": str(rate_bytes_per_second),
    }
    return machine_step(TARGET_CONTAINER, "fetch", approach, environment)


def measure_fork(
    approach: str,
    repetition: int,
    session_id: str,
    nonce: str,
    base_commit: str,
    rate_bytes_per_second: int,
    source_manifest: dict,
) -> ForkMeasurement:
    session_environment = {
        "SESSION_ID": session_id,
        "WORKER_BRANCH": WORKER_BRANCH,
        "BASE_COMMIT": base_commit,
    }
    machine_step(TARGET_CONTAINER, "reset", approach, {})
    checkpoint = machine_step(
        SOURCE_CONTAINER, "checkpoint", approach, session_environment
    )
    file_sizes = checkpoint_file_sizes(checkpoint)
    fetch = fetch_to_target(approach, file_sizes, rate_bytes_per_second)
    restore = machine_step(TARGET_CONTAINER, "restore", approach, session_environment)
    resume = machine_step(
        TARGET_CONTAINER,
        "resume",
        approach,
        {**session_environment, "FORK_PROMPT": FORK_PROMPT},
    )
    mock_log = read_mock_log()
    fork_requests = [
        entry
        for entry in mock_log
        if entry.get("session_header") == resume["fork_session_id"]
    ]
    first_request = fork_requests[0]
    source_requests = [
        entry
        for entry in mock_log
        if entry.get("session_header") == session_id and "body" in entry
    ]
    first_request_text = json.dumps(first_request["body"]["messages"])
    conversation_bytes = file_sizes.pop("conversation.tar.zst")
    return ForkMeasurement(
        approach=approach,
        repetition=repetition,
        checkpoint_seconds=float(checkpoint["checkpoint_finished_at"])
        - float(checkpoint["checkpoint_started_at"]),
        transfer_seconds=float(fetch["fetch_finished_at"])
        - float(fetch["fetch_started_at"]),
        restore_seconds=float(restore["restore_finished_at"])
        - float(restore["restore_started_at"]),
        resume_to_first_tool_call_seconds=float(resume["first_tool_call_at"])
        - float(resume["resume_started_at"]),
        resume_to_first_model_request_seconds=first_request["time_seconds"]
        - float(resume["resume_started_at"]),
        total_seconds=float(resume["first_tool_call_at"])
        - float(checkpoint["checkpoint_started_at"]),
        sandbox_checkpoint_bytes=sum(file_sizes.values()),
        conversation_checkpoint_bytes=conversation_bytes,
        fidelity=compare_manifests(source_manifest, read_manifest(TARGET_CONTAINER)),
        recalled_nonce_matches=resume["recalled_nonce"] == nonce,
        history_reached_model=first_request["reply_kind"] == "tool_use"
        and nonce in first_request["bash_command"]
        and FIRST_PROMPT.split("[[")[0].strip() in first_request_text,
        prompt_prefix_break=prompt_prefix_break(
            source_requests[-1]["body"], first_request["body"]
        ),
        spilled_tool_results_on_target=int(resume["spilled_tool_results"]),
    )


def check_context_usage(session_id: str) -> dict[str, int | float | bool]:
    """Compare the usage Claude Code wrote into the transcript with the usage the mock returned."""
    usage_by_request_id = {
        f"req_mock_{entry['message_number']}": entry.get("usage")
        for entry in read_mock_log()
    }
    transcript = docker(
        ["exec", SOURCE_CONTAINER, "cat", f"{TRANSCRIPT_DIRECTORY}/{session_id}.jsonl"]
    )
    first_session = json.loads(
        docker(["exec", SOURCE_CONTAINER, "cat", "/vol/first_session.json"])
    )
    context_window_tokens = max(
        model["contextWindow"] for model in first_session["modelUsage"].values()
    )
    context_fields = (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    matches: list[bool] = []
    context_tokens = 0
    for line in transcript.splitlines():
        entry = json.loads(line)
        if entry.get("type") == "assistant":
            usage = entry["message"]["usage"]
            context_tokens = sum(usage[name] for name in context_fields)
            returned_usage = usage_by_request_id[entry["requestId"]]
            matches.append(
                context_tokens == sum(returned_usage[name] for name in context_fields)
            )
        else:
            pass
    return {
        "assistant_entries": len(matches),
        "transcript_matches_returned_usage": bool(matches) and all(matches),
        "last_context_tokens": context_tokens,
        "context_window_tokens": context_window_tokens,
        "last_context_fraction": context_tokens / context_window_tokens,
        "transcript_bytes": len(transcript.encode()),
    }


def check_agent_sdk(
    session_id: str, nonce: str, rate_bytes_per_second: int
) -> dict[str, str | int | float | bool]:
    """Fork the same session on machine B through the Agent SDK, with the transcript only in a SessionStore."""
    export = machine_step(
        SOURCE_CONTAINER, "sdk-export", "sdk_store", {"SESSION_ID": session_id}
    )
    file_sizes = checkpoint_file_sizes(export)
    fetch_to_target("sdk_store", file_sizes, rate_bytes_per_second)
    resume = machine_step(
        TARGET_CONTAINER,
        "sdk-resume",
        "sdk_store",
        {"SESSION_ID": session_id, "FORK_PROMPT": FORK_PROMPT},
    )
    return {
        "store_bytes": sum(file_sizes.values()),
        "export_seconds": float(export["checkpoint_finished_at"])
        - float(export["checkpoint_started_at"]),
        "resume_to_first_tool_call_seconds": float(resume["first_tool_call_at"])
        - float(resume["resume_started_at"]),
        "recalled_nonce_matches": resume["recalled_nonce"] == nonce,
        "fork_session_id_differs": resume["fork_session_id"] not in ("", session_id),
        "spilled_tool_results_on_target": int(resume["spilled_tool_results"]),
    }


def summarize(
    measurements: list[ForkMeasurement],
) -> list[dict[str, str | int | float | bool]]:
    rows: list[dict[str, str | int | float | bool]] = []
    for approach in APPROACHES:
        runs = [
            measurement
            for measurement in measurements
            if measurement.approach == approach
        ]
        snapshot_bytes = statistics.median(
            run.sandbox_checkpoint_bytes + run.conversation_checkpoint_bytes
            for run in runs
        )
        total_seconds = statistics.median(run.total_seconds for run in runs)
        is_faithful = all(
            run.fidelity.differing_paths_outside_caches == 0
            and run.fidelity.git_state_matches
            for run in runs
        )
        conversation_ok = all(
            run.recalled_nonce_matches and run.history_reached_model for run in runs
        )
        rows.append(
            {
                "approach": approach,
                "runs": len(runs),
                "median_total_seconds": total_seconds,
                "median_checkpoint_seconds": statistics.median(
                    run.checkpoint_seconds for run in runs
                ),
                "median_transfer_seconds": statistics.median(
                    run.transfer_seconds for run in runs
                ),
                "median_restore_seconds": statistics.median(
                    run.restore_seconds for run in runs
                ),
                "median_resume_seconds": statistics.median(
                    run.resume_to_first_tool_call_seconds for run in runs
                ),
                "median_snapshot_megabytes": snapshot_bytes / BYTES_PER_MEGABYTE,
                "files_faithful": is_faithful,
                "conversation_ok": conversation_ok,
                "passes": total_seconds < PASS_SECONDS
                and snapshot_bytes < PASS_BYTES
                and is_faithful
                and conversation_ok,
            }
        )
    return rows


def markdown_table(rows: list[dict[str, str | int | float | bool]]) -> str:
    header = (
        "| Approach | Total (s) | Checkpoint (s) | Transfer (s) | Restore (s) | Resume (s) | Snapshot (MB) "
        "| Files faithful | Conversation | Passes H11 |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [
        f"| {row['approach']} | {row['median_total_seconds']:.1f} | {row['median_checkpoint_seconds']:.1f} "
        f"| {row['median_transfer_seconds']:.1f} | {row['median_restore_seconds']:.1f} | {row['median_resume_seconds']:.2f} "
        f"| {row['median_snapshot_megabytes']:.0f} | {'yes' if row['files_faithful'] else 'no'} "
        f"| {'yes' if row['conversation_ok'] else 'no'} | {'yes' if row['passes'] else 'no'} |"
        for row in rows
    ]
    return "\n".join([header, *lines])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-megabytes",
        type=int,
        default=300,
        help="Size of the sweep outputs in the sandbox",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--transfer-megabytes-per-second",
        type=int,
        default=100,
        help="Network rate from A to B",
    )
    parser.add_argument("--keep-containers", action="store_true")
    parser.add_argument(
        "--output-directory", type=Path, default=SPIKE_DIRECTORY / "results"
    )
    parsed = parser.parse_args()
    repository_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=SPIKE_DIRECTORY,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    rate_bytes_per_second = parsed.transfer_megabytes_per_second * BYTES_PER_MEGABYTE
    session_id = str(uuid.uuid4())
    nonce = f"NONCE-{secrets.token_hex(16)}"
    try:
        base_commit = start_machines(repository_root)
        command_log = f"export NONCE={nonce}\nexport ARTIFACT_MEGABYTES={parsed.artifact_megabytes}\n"
        command_log += (SPIKE_DIRECTORY / "worker_commands.sh").read_text()
        docker(
            ["exec", "-i", SOURCE_CONTAINER, "bash", "-c", "cat > /vol/command_log.sh"],
            input_text=command_log,
        )
        session = machine_step(
            SOURCE_CONTAINER,
            "session",
            "",
            {"SESSION_ID": session_id, "FIRST_PROMPT": FIRST_PROMPT},
        )
        source_manifest = read_manifest(SOURCE_CONTAINER)
        context_usage = check_context_usage(session_id)
        measurements: list[ForkMeasurement] = []
        for repetition in range(parsed.repetitions):
            for approach in APPROACHES:
                measurement = measure_fork(
                    approach,
                    repetition,
                    session_id,
                    nonce,
                    base_commit,
                    rate_bytes_per_second,
                    source_manifest,
                )
                print(
                    f"{approach} #{repetition}: {measurement.total_seconds:.1f} s, fidelity {measurement.fidelity}"
                )
                measurements.append(measurement)
        sdk_check = check_agent_sdk(session_id, nonce, rate_bytes_per_second)
        claude_version = docker(
            ["run", "--rm", IMAGE_NAME, "claude", "--version"]
        ).strip()
    finally:
        if parsed.keep_containers:
            pass
        else:
            remove_machines()
    summary = summarize(measurements)
    results = {
        "settings": {
            "artifact_megabytes": parsed.artifact_megabytes,
            "repetitions": parsed.repetitions,
            "transfer_megabytes_per_second": parsed.transfer_megabytes_per_second,
            "claude_code_version": claude_version,
            "base_commit": base_commit,
        },
        "session": {
            "sandbox_build_seconds": float(session["sandbox_build_finished_at"])
            - float(session["sandbox_build_started_at"]),
            "sandbox_bytes": int(session["sandbox_bytes"]),
            "sandbox_without_venv_bytes": int(session["sandbox_without_venv_bytes"]),
            "sandbox_paths": len(source_manifest["tree"]),
        },
        "context_usage": context_usage,
        "agent_sdk": sdk_check,
        "summary": summary,
        "measurements": [asdict(measurement) for measurement in measurements],
    }
    parsed.output_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = parsed.output_directory / f"h11-{stamp}.json"
    output_path.write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: results[key]
                for key in ("settings", "session", "context_usage", "agent_sdk")
            },
            indent=2,
        )
    )
    print(markdown_table(summary))
    print(f"Results written to {output_path}")


if __name__ == "__main__":
    main()
