"""A worker with no model: the real MCP herd tools, a scripted job (D61, H12).

It runs inside a sandbox like any agent, started by any executor:
    python -m vlah.testing.scripted_worker --script submit_one | sleep | probe

The harness and the executor say where and who through the environment:
VLAH_TOOL_URL and VLAH_TOOL_TOKEN (the herd's MCP tool server), VLAH_AGENT,
VLAH_TASK, VLAH_TASK_KIND, VLAH_TASK_REFS, VLAH_APPROACH, VLAH_SOURCES (the judge
levels to predict), VLAH_SANDBOX, VLAH_TARGET, VLAH_BLACKBOARD and VLAH_TRANSCRIPT.

submit_one does what the smoke test's toy worker does, over the wire: on an advance
task it writes one candidate, asks for one judge run and records an insight; on a
result follow-up it records a dead end that cites the result. Nothing in it reads
the clock or a random generator, so two runs with the same inputs write the same
claims, and a herd of these workers gives the same event sequence on every
executor. sleep stays silent until killed, for the heartbeat checks. probe reports
what the sandbox can reach, for check_worker_boundary.

The transcript is one JSON object (ScriptedTranscript), rewritten after every tool
call, so the harness can read usage and recent tool calls as it does for a model.
"""

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx2
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from pydantic import JsonValue

from vlah.core.types.base import FrozenModel
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, ClaimType, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId, format_claim_id

MODULE = "vlah.testing.scripted_worker"
Script = Literal["submit_one", "sleep", "probe"]
SCRIPTS: tuple[Script, ...] = ("submit_one", "sleep", "probe")

TOOL_URL_VARIABLE = "VLAH_TOOL_URL"
TOOL_TOKEN_VARIABLE = "VLAH_TOOL_TOKEN"
SCRIPT_VARIABLE = "VLAH_SCRIPT"
APPROACH_VARIABLE = "VLAH_APPROACH"
TASK_KIND_VARIABLE = "VLAH_TASK_KIND"
TASK_REFS_VARIABLE = "VLAH_TASK_REFS"
SOURCES_VARIABLE = "VLAH_SOURCES"
WINDOW_TOKENS_VARIABLE = "VLAH_WINDOW_TOKENS"
PUSH_TOKEN_VARIABLE = "VLAH_PUSH_TOKEN"

DEFAULT_WINDOW_TOKENS = 200_000
# A stand-in for context use: the harness reads it, the loop compares it to its threshold.
TOKENS_PER_TOOL_CALL = 1000
TOOL_TIMEOUT_SECONDS = 60.0
SLEEP_STEP_SECONDS = 3600.0
# The candidate the in-memory judge (vlah.adapters.memory.signal_source) can score.
CANDIDATE_FILE = "policy.py"
SCORE_FILE = "fake_score.json"
SEED_COUNT = 5
CANDIDATES_DIRECTORY = "candidates"
BASELINE_ZERO = "baseline@0"
# Claim numbers are task number times this plus the claim's number in the task, so
# a worker's claims stay unique across its tasks without reading the blackboard.
CLAIMS_PER_TASK = 100
TRAILING_NUMBER = re.compile(r"(\d+)$")

CREDENTIAL_NAME = re.compile(r"(?i)(token|secret|password|_key$|^key$)")
HERD_CONFIG_FILE = "herd.yaml"
PRIVATE_DIRECTORY = "private"
# A private/ counts as a problem's only beside these; /etc/ssl/private is not one.
PROBLEM_FOLDER_SIBLINGS = frozenset({HERD_CONFIG_FILE, "charter", "approaches", "eval"})
PRUNED_DIRECTORIES = frozenset({"proc", "sys", "dev"})
PROBE_TIMEOUT_SECONDS = 5.0
GIT_TIMEOUT_SECONDS = 30.0
HTTPS_PORT = 443


class ScriptedTranscript(FrozenModel):
    """What the program leaves for its harness: usage, tool calls, interrupts seen."""

    used_tokens: int = 0
    window_tokens: int = DEFAULT_WINDOW_TOKENS
    tool_calls: tuple[str, ...] = ()
    interrupts: tuple[str, ...] = ()


def read_transcript(transcript_path: Path) -> ScriptedTranscript:
    """The transcript, or an empty one before the program's first write."""
    if not transcript_path.is_file():
        return ScriptedTranscript()
    else:
        text = transcript_path.read_text(encoding="utf-8")
        if text.strip():
            return ScriptedTranscript.model_validate_json(text)
        else:
            return ScriptedTranscript()


def trailing_number(text: str) -> int:
    match = TRAILING_NUMBER.search(text)
    if match is None:
        raise ValueError(f"{text!r} ends in no number")
    else:
        return int(match.group(1))


@dataclass(kw_only=True)
class ScriptedWorkerSession:
    """Who the program is and where it works, from the environment."""

    agent: ActorId
    task_id: str
    task_kind: str
    task_refs: tuple[str, ...]
    approach: str
    sources: tuple[str, ...]
    sandbox: Path
    transcript_path: Path | None
    tool_url: str
    tool_token: str
    window_tokens: int
    tool_calls: list[str] = field(default_factory=list[str])
    interrupts: list[str] = field(default_factory=list[str])

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "ScriptedWorkerSession":
        required = (TOOL_URL_VARIABLE, TOOL_TOKEN_VARIABLE, "VLAH_AGENT", "VLAH_TASK")
        missing = [name for name in required if not environment.get(name)]
        if missing:
            raise ValueError(f"the scripted worker needs {missing} in its environment")
        else:
            transcript = environment.get("VLAH_TRANSCRIPT")
            return cls(
                agent=ActorId(environment["VLAH_AGENT"]),
                task_id=environment["VLAH_TASK"],
                task_kind=environment.get(TASK_KIND_VARIABLE, "advance"),
                task_refs=tuple(
                    ref
                    for ref in environment.get(TASK_REFS_VARIABLE, "").split(",")
                    if ref
                ),
                approach=environment.get(APPROACH_VARIABLE, "a-01"),
                sources=tuple(
                    source
                    for source in environment.get(SOURCES_VARIABLE, "").split(",")
                    if source
                ),
                sandbox=Path(environment.get("VLAH_SANDBOX", os.getcwd())),
                transcript_path=Path(transcript) if transcript else None,
                tool_url=environment[TOOL_URL_VARIABLE],
                tool_token=environment[TOOL_TOKEN_VARIABLE],
                window_tokens=int(
                    environment.get(WINDOW_TOKENS_VARIABLE, DEFAULT_WINDOW_TOKENS)
                ),
            )

    @property
    def worker_number(self) -> int:
        return trailing_number(self.agent)

    @property
    def task_number(self) -> int:
        return trailing_number(self.task_id)

    def write_transcript(self) -> None:
        if self.transcript_path is None:
            return
        else:
            transcript = ScriptedTranscript(
                used_tokens=len(self.tool_calls) * TOKENS_PER_TOOL_CALL,
                window_tokens=self.window_tokens,
                tool_calls=tuple(self.tool_calls),
                interrupts=tuple(self.interrupts),
            )
            self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.transcript_path.with_suffix(".partial")
            partial.write_text(transcript.model_dump_json(), encoding="utf-8")
            partial.replace(self.transcript_path)

    def record(self, tool_name: str, reply: dict[str, JsonValue]) -> None:
        self.tool_calls.append(tool_name)
        interrupt = reply.get("interrupt")
        if isinstance(interrupt, dict):
            self.interrupts.append(json.dumps(interrupt, sort_keys=True))
        else:
            pass
        self.write_transcript()


async def call_tool(
    session: ScriptedWorkerSession, tool_name: str, arguments: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """One herd tool call over MCP; the reply is a ToolReply as JSON."""
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {session.tool_token}"},
        timeout=TOOL_TIMEOUT_SECONDS,
    ) as http_client:
        async with streamable_http_client(
            session.tool_url, http_client=http_client
        ) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                result = await client.call_tool(tool_name, arguments)
    if not isinstance(result, CallToolResult):
        raise RuntimeError(f"{tool_name} answered with {type(result).__name__}")
    elif result.is_error:
        raise RuntimeError(f"{tool_name} failed: {result.content}")
    elif not isinstance(result.structured_content, dict):
        raise RuntimeError(f"{tool_name} returned no structured reply")
    else:
        reply = {str(key): value for key, value in result.structured_content.items()}
        session.record(tool_name, reply)
        return reply


def tool_result(reply: dict[str, JsonValue]) -> dict[str, JsonValue]:
    result = reply.get("result")
    if isinstance(result, dict):
        return {str(key): value for key, value in result.items()}
    else:
        raise RuntimeError(f"a tool reply carried no result: {reply}")


def write_candidate(session: ScriptedWorkerSession) -> str:
    """The toy candidate of the smoke test, as a directory in the sandbox."""
    relative = f"{CANDIDATES_DIRECTORY}/{session.task_id}"
    directory = session.sandbox / relative
    directory.mkdir(parents=True, exist_ok=True)
    number = session.worker_number
    (directory / CANDIDATE_FILE).write_text(f"WORKER = {number}\n", encoding="utf-8")
    (directory / SCORE_FILE).write_text(
        json.dumps({"per_seed": [float(number + seed) for seed in range(SEED_COUNT)]}),
        encoding="utf-8",
    )
    manifest = CandidateManifest(
        kind="artifact",
        target=BASELINE_ZERO,
        approach=session.approach,
        author=session.agent,
        created_at=0.0,
    )
    (directory / "manifest.yaml").write_text(
        yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return relative


async def write_claim(
    session: ScriptedWorkerSession,
    number_in_task: int,
    claim_type: ClaimType,
    body: str,
    evidence: EvidenceRef,
) -> str:
    claim = Claim.model_validate(
        {
            "id": format_claim_id(
                session.agent, session.task_number * CLAIMS_PER_TASK + number_in_task
            ),
            "type": claim_type,
            "status": "proposed",
            "approach": session.approach,
            "author": session.agent,
            "evidence": [evidence.model_dump(exclude_none=True)],
            "body": body,
        }
    )
    reply = await call_tool(
        session,
        "repo_write",
        {"files": {f"claims/{session.agent}/{claim.id}.md": render_claim(claim)}},
    )
    error = tool_result(reply).get("error")
    if isinstance(error, str):
        raise RuntimeError(f"claim {claim.id} was refused: {error}")
    else:
        return claim.id


async def submit_one(session: ScriptedWorkerSession) -> None:
    if session.task_kind == "advance":
        number = session.worker_number
        arguments: dict[str, JsonValue] = {
            "candidate_dir": write_candidate(session),
            "predictions": {source: float(number) for source in session.sources},
            "rationale": f"{session.agent} raises every seed by {number} (scripted)",
        }
        result = tool_result(await call_tool(session, "eval_request", arguments))
        if result.get("status") == "needs_confirmation":
            arguments["confirm_token"] = result.get("confirm_token")
            result = tool_result(await call_tool(session, "eval_request", arguments))
        else:
            pass
        claim_id = result.get("claim_id")
        evidence = (
            EvidenceRef(claim=claim_id)
            if isinstance(claim_id, str)
            else EvidenceRef(path="spec.md")
        )
        await write_claim(
            session,
            1,
            "insight",
            "Submitted one candidate and asked for one judge run (scripted worker).",
            evidence,
        )
    elif session.task_kind == "result_followup":
        if not session.task_refs:
            raise ValueError("a result follow-up names the result claim in its refs")
        else:
            await write_claim(
                session,
                1,
                "dead_end",
                "The scripted candidate is done; nothing further to try.",
                EvidenceRef(claim=session.task_refs[0]),
            )
    else:
        raise ValueError(
            f"the scripted worker has no plan for a {session.task_kind} task"
        )


def sleep_forever(session: ScriptedWorkerSession) -> None:
    session.write_transcript()
    while True:
        time.sleep(SLEEP_STEP_SECONDS)


def attempt_fetch(url: str) -> str:
    """reached when the URL answers with any HTTP status; blocked otherwise."""
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError:
        return "reached"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return f"blocked: {type(error).__name__}"
    return "reached"


def attempt_raw_connection(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
            pass
    except OSError as error:
        return f"blocked: {type(error).__name__}"
    return "connected"


def attempt_push(push_url: str | None, clone: Path | None) -> str:
    if push_url is None or clone is None or not clone.is_dir():
        return "skipped"
    else:
        completed = subprocess.run(
            ["git", "-C", str(clone), "push", push_url, "HEAD:refs/heads/main"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode == 0:
            return "pushed"
        else:
            return f"rejected: {completed.stderr.strip()[-200:]}"


def git_remotes(clone: Path | None) -> str:
    if clone is None or not clone.is_dir():
        return "absent"
    else:
        completed = subprocess.run(
            ["git", "-C", str(clone), "remote", "-v"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        return completed.stdout.strip()


def search_filesystem(
    search_roots: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Every herd.yaml, and every private/ that sits in a problem folder, under the roots."""
    herd_configs: list[str] = []
    private_directories: list[str] = []
    for root in search_roots:
        for directory_path, directory_names, file_names in os.walk(root):
            directory_names[:] = [
                name for name in directory_names if name not in PRUNED_DIRECTORIES
            ]
            if HERD_CONFIG_FILE in file_names:
                herd_configs.append(str(Path(directory_path) / HERD_CONFIG_FILE))
            else:
                pass
            if PRIVATE_DIRECTORY in directory_names and PROBLEM_FOLDER_SIBLINGS & {
                *directory_names,
                *file_names,
            }:
                private_directories.append(
                    str(Path(directory_path) / PRIVATE_DIRECTORY)
                )
            else:
                pass
    return herd_configs, private_directories


def probe(
    environment: Mapping[str, str],
    tool_url: str,
    blocked_host: str,
    public_address: str,
    hidden_paths: tuple[str, ...],
    search_roots: tuple[str, ...],
    push_url: str | None,
    allowed_variables: tuple[str, ...],
) -> dict[str, JsonValue]:
    """What this sandbox can see and reach; check_worker_boundary reads the answer."""
    herd_configs, private_directories = search_filesystem(search_roots)
    target = environment.get("VLAH_TARGET")
    blackboard = environment.get("VLAH_BLACKBOARD")
    blackboard_clone = Path(blackboard) if blackboard else None
    return {
        "hidden_readable": [path for path in hidden_paths if Path(path).exists()],
        "herd_yaml_found": [path for path in herd_configs],
        "private_found": [path for path in private_directories],
        "credential_variables": [
            name
            for name in sorted(environment)
            if CREDENTIAL_NAME.search(name) and name not in allowed_variables
        ],
        "push_token_present": PUSH_TOKEN_VARIABLE in environment,
        "blocked_via_proxy": attempt_fetch(f"http://{blocked_host}/"),
        "raw_public_address": attempt_raw_connection(public_address, HTTPS_PORT),
        "tool_url": attempt_fetch(tool_url),
        "git_push": attempt_push(push_url, blackboard_clone),
        "git_remotes": {
            "target": git_remotes(Path(target) if target else None),
            "blackboard": git_remotes(blackboard_clone),
        },
    }


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="A scripted herd worker: the real MCP tools, no model.",
    )
    parser.add_argument(
        "--script",
        choices=SCRIPTS,
        default=os.environ.get(SCRIPT_VARIABLE, "submit_one"),
        help="what to do (default: VLAH_SCRIPT, else submit_one)",
    )
    probe_group = parser.add_argument_group("probe")
    probe_group.add_argument("--tool-url", default=os.environ.get(TOOL_URL_VARIABLE))
    probe_group.add_argument("--blocked-host", default="blocked.example.invalid")
    probe_group.add_argument("--public-address", default="1.1.1.1")
    probe_group.add_argument("--hidden-path", action="append", default=[])
    probe_group.add_argument("--search-root", action="append", default=[])
    probe_group.add_argument("--push-url", default=None)
    probe_group.add_argument(
        "--allowed-variable", action="append", default=[TOOL_TOKEN_VARIABLE]
    )
    probe_group.add_argument(
        "--output", default=None, help="also write the probe report to this file"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    if arguments.script == "probe":
        if arguments.tool_url is None:
            raise SystemExit("probe needs --tool-url or VLAH_TOOL_URL")
        else:
            pass
        report = probe(
            os.environ,
            tool_url=arguments.tool_url,
            blocked_host=arguments.blocked_host,
            public_address=arguments.public_address,
            hidden_paths=tuple(arguments.hidden_path),
            search_roots=tuple(arguments.search_root),
            push_url=arguments.push_url,
            allowed_variables=tuple(arguments.allowed_variable),
        )
        text = json.dumps(report, sort_keys=True)
        if arguments.output is not None:
            Path(arguments.output).write_text(text + "\n", encoding="utf-8")
        else:
            pass
        print(text)
        return 0
    else:
        session = ScriptedWorkerSession.from_environment(os.environ)
        if arguments.script == "sleep":
            sleep_forever(session)
        else:
            asyncio.run(submit_one(session))
        return 0


if __name__ == "__main__":
    sys.exit(main())
else:
    pass
