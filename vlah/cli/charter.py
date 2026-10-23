"""Scaffold problem folders without choosing a human's experiment settings."""

import subprocess
from pathlib import Path

import yaml

from vlah.core.problem.loader import load_problem

CHARTER_PROMPTS = {
    "problem.md": ("Problem", "Describe the problem and its evaluation interface."),
    "why.md": ("Why", "Explain why this problem and a better result matter."),
    "progress.md": (
        "Progress",
        "Define progress, noise and an optional yaml target block.",
    ),
    "taste.md": ("Taste", "Describe preferences and what would count as gaming."),
    "non_goals.md": ("Non-goals", "List what is explicitly outside this run."),
    "escalation.md": ("Escalation", "List decisions that require a human."),
    "assumptions.md": (
        "Assumptions",
        "Confirm or reject the assumptions behind the run.",
    ),
    "disagreements.md": (
        "Disagreements",
        "Record unresolved disagreements explicitly.",
    ),
    "egress.md": ("Egress", "Explain the hosts allowed by egress.yaml."),
}


def init_problem(problem_root: Path, target_repository: Path) -> Path:
    """Create a draft without copying the target repository or changing its files."""
    repository = target_repository.resolve(strict=True)
    repository_check = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if repository_check.returncode != 0:
        raise ValueError(f"{repository}: --repo: expected a git working tree")
    else:
        repository = Path(repository_check.stdout.strip())
    root = problem_root.absolute()
    root.mkdir(parents=True, exist_ok=False)
    for directory in (
        "charter",
        "approaches",
        "eval/sources",
        "map/measurements",
        "private",
    ):
        (root / directory).mkdir(parents=True)
    for name, (title, prompt) in CHARTER_PROMPTS.items():
        (root / "charter" / name).write_text(
            f"# {title}\n\n<!-- TODO (#23): {prompt} -->\n", encoding="utf-8"
        )
    files = {
        "charter/status.md": "draft\n",
        "charter/params.yaml": (
            "# Defaults come from Params. Set fixed slots_per_approach by hand (#23).\n"
            "{}\n"
        ),
        "charter/constraints.md": (
            "# Constraints\n\n"
            "Replace the null values with your approved limits before signing (#23).\n"
            "Add per-source total run caps under runs after declaring your sources.\n\n"
            "```yaml limits\n"
            "eval_machine_hours: null\n"
            "days: null\n"
            "runs: {}\n"
            "```\n"
        ),
        "charter/gates.yaml": "# Add hard constraints as GateSpec entries (#23).\n[]\n",
        "charter/egress.yaml": (
            "# Add approved context and package-mirror hosts (#23).\nallow: []\n"
        ),
        "charter/sources.md": (
            "# Sources\n\n"
            "For each eval/sources/<id>.yaml, copy its exact id below and confirm "
            "its trust and readable_by values.\n\n"
            "<!-- TODO (#23): Replace this example with one checked entry per source.\n"
            "- [ ] SOURCE_ID: trust=..., readable_by=[...]\n"
            "-->\n"
        ),
        "charter/transcript.ptr": "",
        "spec.md": (
            "# Problem specification\n\n"
            "<!-- TODO (#23, @minyi-tang): Write the problem and eval interface. -->\n"
        ),
        "map/gap.md": (
            "# Opportunity map\n\n"
            "<!-- TODO (#23, @minyi-tang): Add initial measurements and explain the gap. -->\n"
        ),
    }
    for relative_path, text in files.items():
        (root / relative_path).write_text(text, encoding="utf-8")
    (root / "herd.yaml").write_text(
        "# Deployment settings are private. Choose the harness and model before running.\n"
        + yaml.safe_dump(
            {
                "judges": {},
                "executor": {
                    "kind": "local",
                    "settings": {"target_repository": str(repository)},
                },
                "harness": {"kind": "TODO"},
                "models": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return root


def sign_problem(problem_root: Path) -> Path:
    """Validate the folder before recording the human's charter approval."""
    problem = load_problem(problem_root)
    status_path = problem.root / "charter/status.md"
    status_path.write_text("aligned\n", encoding="utf-8")
    return status_path
