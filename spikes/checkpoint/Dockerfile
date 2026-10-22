# One image plays every "machine" in the H11 spike: worker A, worker B and the mock model API.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.308
ARG CLAUDE_AGENT_SDK_VERSION=0.2.172

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git procps python3 python3-venv util-linux zstd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /usr/local/bin/

RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && claude --version

RUN uv venv /opt/sdk --python /usr/bin/python3 \
    && uv pip install --python /opt/sdk/bin/python claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}

ENV DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    UV_LINK_MODE=copy

COPY . /opt/h11/
