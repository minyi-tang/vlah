# The command log of one worker session on testbed problem A, in the order the worker ran it.
# Machine A runs it to build the sandbox; the command_replay approach replays it on machine B.
# The harness prepends the NONCE and ARTIFACT_MEGABYTES values a recorder would have captured.
set -euo pipefail
cd /work/repo
git config user.name "worker w1"
git config user.email "w1@herd.invalid"
git switch -q -c worker/w1
uv venv -q --python /usr/bin/python3 .venv
uv pip install -q --python .venv/bin/python \
    simpy==4.1.1 numpy==2.3.4 pandas==2.3.3 scipy==1.16.2 matplotlib==3.10.7
mkdir -p .h11 policy
echo "$NONCE" > .h11/nonce.txt
# Write and Edit tool calls are file writes, so the recorder has to log their content too.
cat > policy/backfill.py <<'POLICY'
"""Backfill: start a short job early when it fits the free cores and ends before the head job starts."""


def can_backfill(job_cores: int, job_runtime_minutes: float, free_cores: int, head_start_minutes: float) -> bool:
    return job_cores <= free_cores and job_runtime_minutes <= head_start_minutes
POLICY
git add policy/backfill.py
git commit -q -m "Add a backfill policy"
git rm -q "$(git ls-files docs | sed -n 1p)"
git commit -q -m "Drop a doc the sweep does not use"
printf '\nBACKFILL_WINDOW_MINUTES = 30\n' >> policy/backfill.py
printf '\nNotes from worker w1: backfill sweep results are in runs/sweep-1.\n' >> README.md
git add README.md
.venv/bin/python /opt/h11/make_artifacts.py --output-directory runs/sweep-1 --megabytes "$ARTIFACT_MEGABYTES" --seed 7
.venv/bin/python -m compileall -q policy
