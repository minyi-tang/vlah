"""Issue #10, section 2: git must track no hidden testbed material (complements the pack test)."""

import re
import subprocess

from testbed.problem_api import REPO_ROOT

FORBIDDEN = re.compile(
    r"(^|/)(hidden|holdout|planted)/|^problems/.*/(probes|reference)/|(^|/)test_hidden\.py$"
    r"|^private/|^problems/sched/problem\.yaml$"
)


def test_git_tracks_no_hidden_material():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    bad = [f for f in tracked.splitlines() if FORBIDDEN.search(f)]
    assert bad == []
