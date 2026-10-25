# The worker image every container and pod executor starts from (L-00, #127).
#
# Holds git, Python with the vlah package (the interrupt hook and the scripted
# worker run inside), Node with the Claude Code and GitHub Copilot CLIs, and a
# non-root user whose home is under /work, the sandbox the executor mounts.
# No credential is baked in: workers get their tokens per session, in the
# environment, and the image test scans the build history for leaks.
#
# Build from the repository root, so the vlah package is in the context:
#     docker build -t vlah-worker:dev -f deploy/worker/Dockerfile .
FROM node:22-bookworm-slim AS toolchain

# Bumped on purpose; the image test runs both CLIs.
ARG CLAUDE_CODE_VERSION=2.1.310
ARG COPILOT_CLI_VERSION=1.0.103

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@github/copilot@${COPILOT_CLI_VERSION}" \
    && npm cache clean --force

FROM toolchain AS packages

COPY pyproject.toml README.md LICENSE /src/
COPY vlah /src/vlah
COPY testbed /src/testbed
COPY problems /src/problems
RUN python3 -m venv /opt/vlah \
    && /opt/vlah/bin/pip wheel --no-cache-dir --no-deps --wheel-dir /wheels /src/testbed /src \
    && printf 'vlah-testbed @ file://%s\n' /wheels/vlah_testbed-*.whl > /wheels/constraints.txt \
    && /opt/vlah/bin/pip install --no-cache-dir --find-links /wheels \
        --constraint /wheels/constraints.txt /wheels/vlah-*.whl \
    && /opt/vlah/bin/pip check \
    && /opt/vlah/bin/python -c "import shutil, testbed; from pathlib import Path; shutil.rmtree(Path(testbed.__file__).parent / 'problems')"

FROM toolchain AS worker

COPY --from=packages /opt/vlah /opt/vlah

RUN useradd --create-home --uid 1001 worker \
    && mkdir -p /work/.home \
    && chown -R worker:worker /work

USER worker
ENV PATH=/opt/vlah/bin:$PATH \
    HOME=/work/.home
WORKDIR /work

CMD ["python", "-m", "vlah.testing.scripted_worker", "--help"]
