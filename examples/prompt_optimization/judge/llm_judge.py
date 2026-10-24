"""A model-graded judge: how well a candidate prompt prepares a support assistant.

    python3 llm_judge.py <candidate_dir> --dataset dev|audit|judge [--env-file PATH]

For each hidden situation in llm_cases.json, an assistant model answers the
customer's message with the candidate's prompt.md as its only system prompt, and a
grader model scores the reply from 0 to 10 against the policy handbook and the
situation's criteria. The score is the mean over situations, divided by 10; the
per-situation scores are per_seed, so the herd's paired comparison works. A prompt
longer than the word limit fails the constraint and scores 0.

Claude 5.5 models accept only the default temperature, so a score carries the
models' own noise; the herd's paired comparison and spot checks are built for that.

The models are reached through an Anthropic Messages API: a LiteLLM gateway by
default (LITELLM_URL and LITELLM_MASTER_KEY, read from --env-file), or any base URL
and key in VLAH_JUDGE_BASE_URL and VLAH_JUDGE_API_KEY. The key is never printed.
Workers never see this file, llm_cases.json or the grader's reasons.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CASES = Path(__file__).resolve().parent / "llm_cases.json"
DEFAULT_ENV_FILE = Path.home() / "litellm-copilot" / ".env"
DEFAULT_BASE_URL = "http://127.0.0.1:4000"
ASSISTANT_MODEL = "claude-haiku-5-5"
GRADER_MODEL = "claude-sonnet-5-5"
ASSISTANT_MAX_TOKENS = 400
GRADER_MAX_TOKENS = 800
# A grader that reasons first can run out of tokens before its JSON: grade again.
GRADER_ATTEMPTS = 3
SCORE_PATTERN = re.compile(r'"score"\s*:\s*(\d+(?:\.\d+)?)')
REQUEST_TIMEOUT_SECONDS = 120
ATTEMPTS = 4
PARALLEL_CALLS = 8
MAX_SCORE = 10.0
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
GRADER_SYSTEM = (
    "You grade one customer-support reply for Northwind Outfitters. You are strict "
    "and consistent. Use only the handbook and the criteria you are given."
)
GRADER_TEMPLATE = """Policy handbook:
{handbook}

Customer message:
{message}

The assistant's reply:
{reply}

What a great reply does here:
{criteria}

Score the reply from 0 to 10. 10: it does everything above, every policy fact it
states is correct, it invents nothing, and it is warm, direct and brief. Take
points off for each wrong or invented fact, each missing step, a promise the
handbook does not allow, a cold or defensive tone, and padding.

Answer with only this JSON object and no other text, before or after it:
{{"score": <0-10>, "reason": "<one sentence>"}}"""


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                name, value = stripped.removeprefix("export ").split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
            else:
                continue
    else:
        pass
    return values


def connection(env_file: Path) -> tuple[str, str]:
    from_file = read_env_file(env_file)
    base_url = (
        os.environ.get("VLAH_JUDGE_BASE_URL")
        or from_file.get("LITELLM_URL")
        or DEFAULT_BASE_URL
    )
    key = os.environ.get("VLAH_JUDGE_API_KEY") or from_file.get("LITELLM_MASTER_KEY")
    if not key:
        raise SystemExit(
            f"no API key: set VLAH_JUDGE_API_KEY or LITELLM_MASTER_KEY in {env_file}"
        )
    else:
        return base_url.rstrip("/"), key


def complete(
    base_url: str, key: str, model: str, system: str, user: str, max_tokens: int
) -> str:
    body = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode("utf-8")
    delay = 2.0
    for attempt in range(ATTEMPTS):
        request = urllib.request.Request(
            f"{base_url}/v1/messages",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as reply:
                data = json.loads(reply.read())
            return "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            ).strip()
        except urllib.error.HTTPError as error:
            if error.code in RETRY_STATUSES and attempt + 1 < ATTEMPTS:
                time.sleep(delay)
                delay *= 2
            else:
                raise RuntimeError(f"{model} answered HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt + 1 < ATTEMPTS:
                time.sleep(delay)
                delay *= 2
            else:
                raise RuntimeError(f"{model} could not be reached: {error}") from error
    raise RuntimeError(f"{model} failed {ATTEMPTS} times")


def grade(
    base_url: str,
    key: str,
    prompt: str,
    handbook: str,
    case: dict,
    models: tuple[str, str],
) -> float:
    reply = complete(
        base_url, key, models[0], prompt, case["message"], ASSISTANT_MAX_TOKENS
    )
    verdict = complete(
        base_url,
        key,
        models[1],
        GRADER_SYSTEM,
        GRADER_TEMPLATE.format(
            handbook=handbook,
            message=case["message"],
            reply=reply or "(no reply)",
            criteria="\n".join(f"- {item}" for item in case["criteria"]),
        ),
        GRADER_MAX_TOKENS,
    )
    score = parse_score(verdict)
    if score is None:
        raise UnreadableGrade(f"the grader's answer has no score: {verdict[:200]!r}")
    else:
        return max(0.0, min(MAX_SCORE, score)) / MAX_SCORE


class UnreadableGrade(RuntimeError):
    pass


def parse_score(verdict: str) -> float | None:
    """The score in the grader's answer: its JSON object, or a "score": n anywhere."""
    for match in re.finditer(r"\{[^{}]*\}", verdict):
        try:
            value = json.loads(match.group(0)).get("score")
        except json.JSONDecodeError:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        else:
            continue
    loose = SCORE_PATTERN.search(verdict)
    return float(loose.group(1)) if loose else None


def grade_with_retries(
    base_url: str,
    key: str,
    prompt: str,
    handbook: str,
    case: dict,
    models: tuple[str, str],
) -> float:
    for attempt in range(GRADER_ATTEMPTS):
        try:
            return grade(base_url, key, prompt, handbook, case, models)
        except UnreadableGrade:
            if attempt + 1 < GRADER_ATTEMPTS:
                continue
            else:
                raise
    raise RuntimeError(f"no readable grade after {GRADER_ATTEMPTS} attempts")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_dir", type=Path)
    parser.add_argument("--dataset", default="dev", choices=["dev", "audit", "judge"])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--assistant-model", default=ASSISTANT_MODEL)
    parser.add_argument("--grader-model", default=GRADER_MODEL)
    arguments = parser.parse_args()
    prompt_path = arguments.candidate_dir / "prompt.md"
    if not prompt_path.is_file():
        print(json.dumps({"score": None, "error": "the candidate has no prompt.md"}))
        return 0
    else:
        pass
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    prompt = prompt_path.read_text(encoding="utf-8")
    words = len(prompt.split())
    situations = cases["audit" if arguments.dataset == "audit" else "dev"]
    if words > cases["max_words"]:
        print(
            json.dumps(
                {
                    "score": 0.0,
                    "per_seed": [0.0] * len(situations),
                    "n": len(situations),
                    "constraint_ok": False,
                    "words": words,
                }
            )
        )
        return 0
    else:
        base_url, key = connection(arguments.env_file)
        models = (arguments.assistant_model, arguments.grader_model)
        with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
            per_case = list(
                pool.map(
                    lambda case: grade_with_retries(
                        base_url, key, prompt, cases["handbook"], case, models
                    ),
                    situations,
                )
            )
        print(
            json.dumps(
                {
                    "score": sum(per_case) / len(per_case),
                    "per_seed": per_case,
                    "n": len(per_case),
                    "constraint_ok": True,
                    "words": words,
                }
            )
        )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f"llm_judge: {error}", file=sys.stderr)
        raise SystemExit(1)
