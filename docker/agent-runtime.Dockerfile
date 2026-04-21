# AWF agent-runtime image — the container that holds the repo worktree and
# the coding CLIs (Codex, Claude Code, Gemini). Built multi-arch for x86_64
# and arm64 (DGX Spark target) via ``docker buildx build --platform=...``.
#
# Build locally:
#   docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
#
# Build multi-arch and push:
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -t ghcr.io/dimileeh/awf-agent-runtime:<tag> \
#     -f docker/agent-runtime.Dockerfile \
#     --push .

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22
ARG DEBIAN_VERSION=bookworm

# ── Stage 1: base OS + system deps ─────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      git \
      gnupg \
      jq \
      libpq-dev \
      openssh-client \
      procps \
      ripgrep \
      tini \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: Node.js (for coding CLIs which are all npm packages) ──────────
ARG NODE_VERSION
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version

# ── Stage 3: coding CLIs ──────────────────────────────────────────────────
#
# Each CLI is pinned to a version. Bump via PR so we can verify the output
# format hasn't drifted in the adapters.
ARG CODEX_VERSION=latest
ARG CLAUDE_CODE_VERSION=latest
ARG GEMINI_VERSION=latest

RUN npm install -g --no-fund --no-audit \
      @openai/codex@${CODEX_VERSION} \
      @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
      @google/gemini-cli@${GEMINI_VERSION} \
    && npm cache clean --force \
    && codex --version || true \
    && claude --version || true \
    && gemini --version || true

# ── Stage 4: Python tooling the agent may need inside the container ────────
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir \
        "alembic>=1.13" \
        "pytest>=8" \
        "psycopg[binary]>=3.1" \
        "uv>=0.5"

# ── Stage 5: non-root user + workspace mount point ─────────────────────────
RUN useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && chown -R agent:agent /workspace

USER agent
WORKDIR /workspace

# tini reaps zombies when the CLI forks subprocesses (common in test runs).
ENTRYPOINT ["/usr/bin/tini", "--"]
# Default command keeps the container alive so ``docker compose exec`` can run
# coding CLIs inside. The adapter layer owns the actual invocations.
CMD ["sh", "-c", "sleep infinity"]
