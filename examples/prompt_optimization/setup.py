"""Create a runnable copy of the prompt-optimization example.

    uv run python examples/prompt_optimization/setup.py <destination> [options]

Copies problem/ to <destination>, puts both judges and their hidden cases under
<destination>/private/ (private/ is never committed, so the repo keeps them in
judge/), creates the target repository at <destination>/target with the initial
prompt committed on main, and writes herd.yaml for the chosen judge and harness:

    --judge rubric        the phrase rubric: free and deterministic (default)
    --judge llm           a model grades each reply; needs a model gateway
    --harness copilot     GitHub Copilot CLI workers (default)
    --harness claude_code Claude Code workers; --claude-command names the CLI or a
                          wrapper such as ~/litellm-copilot/claude-ghcp, --claude-bin
                          the claude binary the wrapper runs, --model-api-url where
                          Claude Code reaches the model
    --model, --effort     the one model for every worker, and its reasoning effort
    --slots N             workers per approach (four approaches)

Nothing else is chosen for you; edit the charter and herd.yaml before running if you
want other limits.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

EXAMPLE_ROOT = Path(__file__).resolve().parent
TARGET_PLACEHOLDER = "__TARGET_REPOSITORY__"
GIT_IDENTITY = ("-c", "user.name=vlah", "-c", "user.email=vlah@example.invalid")
JUDGE_COMMANDS = {
    "rubric": "python3 {problem_root}/private/judge.py {candidate_dir} --dataset {dataset}",
    "llm": "python3 {problem_root}/private/llm_judge.py {candidate_dir} --dataset {dataset}",
}
DEFAULT_MODELS = {"copilot": "gpt-6.1-sol", "claude_code": "claude-opus-5-5"}


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *GIT_IDENTITY, *arguments],
        check=True,
        capture_output=True,
    )


def harness_block(arguments: argparse.Namespace) -> dict[str, object]:
    settings: dict[str, object] = {}
    if arguments.harness == "claude_code":
        if arguments.claude_command:
            settings["command"] = [str(Path(arguments.claude_command).expanduser())]
        else:
            pass
        if arguments.claude_bin:
            settings["environment"] = {
                "CLAUDE_BIN": str(Path(arguments.claude_bin).expanduser())
            }
        else:
            pass
        if arguments.model_api_url:
            settings["model_api_urls"] = [arguments.model_api_url]
        else:
            pass
        if arguments.effort:
            settings["effort"] = arguments.effort
        else:
            pass
    elif arguments.effort:
        settings["reasoning_effort"] = arguments.effort
    else:
        pass
    return {"kind": arguments.harness, "settings": settings}


def write_herd_config(
    destination: Path, target: Path, arguments: argparse.Namespace
) -> None:
    herd_path = destination / "herd.yaml"
    text = herd_path.read_text(encoding="utf-8").replace(
        TARGET_PLACEHOLDER, str(target.resolve())
    )
    config: object = yaml.safe_load(text)
    assert isinstance(config, dict)
    config["judges"] = {"rubric": {"command": JUDGE_COMMANDS[arguments.judge]}}
    config["harness"] = harness_block(arguments)
    config["models"] = {"worker": arguments.model or DEFAULT_MODELS[arguments.harness]}
    herd_path.write_text(
        "# Deployment settings. Workers never see this file.\n"
        + yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def write_slots(destination: Path, slots: int) -> None:
    params_path = destination / "charter" / "params.yaml"
    params: object = yaml.safe_load(params_path.read_text(encoding="utf-8"))
    assert isinstance(params, dict)
    params["slots_per_approach"] = {
        approach: slots for approach in params["slots_per_approach"]
    }
    params_path.write_text(yaml.safe_dump(params, sort_keys=False), encoding="utf-8")


def create(destination: Path, arguments: argparse.Namespace) -> Path:
    if destination.exists():
        raise SystemExit(f"{destination} exists; choose a new folder")
    else:
        pass
    shutil.copytree(EXAMPLE_ROOT / "problem", destination)
    shutil.copytree(EXAMPLE_ROOT / "judge", destination / "private")
    target = destination / "target"
    target.mkdir()
    shutil.copy2(EXAMPLE_ROOT / "target" / "prompt.md", target / "prompt.md")
    git(target, "init", "-q", "-b", "main")
    git(target, "add", ".")
    git(target, "commit", "-qm", "The initial support prompt")
    write_herd_config(destination, target, arguments)
    write_slots(destination, arguments.slots)
    return destination


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("destination", type=Path)
    parser.add_argument("--judge", choices=sorted(JUDGE_COMMANDS), default="rubric")
    parser.add_argument("--harness", choices=sorted(DEFAULT_MODELS), default="copilot")
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--claude-command")
    parser.add_argument("--claude-bin")
    parser.add_argument("--model-api-url")
    parser.add_argument("--slots", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    arguments = parse(argv)
    destination = create(arguments.destination.expanduser().resolve(), arguments)
    workers = arguments.slots * 4
    print(f"Created {destination}")
    print(
        f"Next: uv run vlah run {destination} --arm S2 --workers {workers} --hours 0.5"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
