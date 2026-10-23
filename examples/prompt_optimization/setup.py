"""Create a runnable copy of the prompt-optimization example.

    uv run python examples/prompt_optimization/setup.py <destination>

Copies problem/ to <destination>, puts the judge and its hidden cases under
<destination>/private/ (private/ is never committed, so the repo keeps them in
judge/), creates the target repository at <destination>/target with the initial
prompt committed on main, and writes the repository's path into herd.yaml. Nothing
else is chosen for you; edit the charter and herd.yaml before running if you want
other limits or another model.
"""

import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLE_ROOT = Path(__file__).resolve().parent
TARGET_PLACEHOLDER = "__TARGET_REPOSITORY__"
GIT_IDENTITY = ("-c", "user.name=vlah", "-c", "user.email=vlah@example.invalid")


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(directory), *GIT_IDENTITY, *arguments],
        check=True,
        capture_output=True,
    )


def create(destination: Path) -> Path:
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
    herd_path = destination / "herd.yaml"
    herd_path.write_text(
        herd_path.read_text(encoding="utf-8").replace(
            TARGET_PLACEHOLDER, str(target.resolve())
        ),
        encoding="utf-8",
    )
    return destination


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    else:
        destination = create(Path(argv[1]).expanduser().resolve())
        print(f"Created {destination}")
        print(f"Next: uv run vlah run {destination} --arm S2 --workers 2 --hours 0.5")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
