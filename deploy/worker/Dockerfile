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
FROM node:22-bookworm-slim

# Bumped on purpose; the image test runs both CLIs.
ARG CLAUDE_CODE_VERSION=2.1.310
ARG COPILOT_CLI_VERSION=1.0.103

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@github/copilot@${COPILOT_CLI_VERSION}" \
    && npm cache clean --force

COPY pyproject.toml README.md LICENSE /src/
COPY vlah /src/vlah
RUN python3 -m venv /opt/vlah \
    && /opt/vlah/bin/pip install --no-cache-dir /src \
    && rm -rf /src

RUN useradd --create-home --uid 1001 worker \
    && mkdir -p /work/.home \
    && chown -R worker:worker /work

USER worker
ENV PATH=/opt/vlah/bin:$PATH \
    HOME=/work/.home
WORKDIR /work

CMD ["python", "-m", "vlah.testing.scripted_worker", "--help"]
