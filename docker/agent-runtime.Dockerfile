# AWF agent-runtime image — the container that holds the repo worktree and
# the coding CLIs (Codex, Claude Code, Gemini, OpenCode). Built multi-arch
# for x86_64 and arm64 (DGX Spark target) via ``docker buildx build
# --platform=...``.
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
      # Playwright's chromium system deps — installed here so the
      # non-root agent user can run ``npx playwright install chromium``
      # WITHOUT the ``--with-deps`` variant (which requires sudo/su to
      # apt-install these packages and fails with "Authentication
      # failure" inside this container).
      fonts-liberation \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libatspi2.0-0 \
      libcairo2 \
      libcups2 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libglib2.0-0 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libx11-xcb1 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxrandr2 \
      libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: Docker CLI + Compose plugin ──────────────────────────────────
ARG DOCKER_CE_CLI_VERSION=5:29.4.1-1~debian.12~bookworm
ARG DOCKER_COMPOSE_PLUGIN_VERSION=5.1.3-1~debian.12~bookworm
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
      -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "docker-ce-cli=${DOCKER_CE_CLI_VERSION}" \
        "docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version \
    && docker compose version

# ── Stage 3: GitHub CLI ───────────────────────────────────────────────────
ARG GH_VERSION=2.92.0
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "gh=${GH_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && gh --version

# ── Stage 4: Node.js (for coding CLIs which are all npm packages) ──────────
ARG NODE_VERSION
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version \
    && npm --version

# ── Stage 5: coding CLIs ──────────────────────────────────────────────────
#
# Each CLI is pinned to a version. Bump via PR so we can verify the output
# format hasn't drifted in the adapters.
ARG CODEX_VERSION=0.130.0
ARG CLAUDE_CODE_VERSION=2.1.143
ARG GEMINI_VERSION=0.42.0
ARG OPENCODE_VERSION=1.15.2
# Usage collector. Pinned (not fetched via runtime npx/bunx) so AWF's
# per-workspace usage sampler reads local provider usage files offline.
ARG CCUSAGE_VERSION=20.0.3

RUN npm install -g --no-fund --no-audit \
      @openai/codex@${CODEX_VERSION} \
      @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
      @google/gemini-cli@${GEMINI_VERSION} \
      opencode-ai@${OPENCODE_VERSION} \
      ccusage@${CCUSAGE_VERSION} \
    && npm cache clean --force \
    && codex --version || true \
    && claude --version || true \
    && gemini --version || true \
    && opencode --version || true \
    && ccusage --version

# Neutral ccusage config consumed via ``--config`` by AWF's usage collector
# (src/awf/service/usage_collection.py). The per-workspace auth copy seeds
# ~/.claude from the host and does not strip a host ccusage.json, so pinning an
# empty config keeps ccusage from auto-discovering a user/project config whose
# since/until/project/breakdown defaults would silently filter per-run usage.
RUN mkdir -p /opt/awf \
    && printf '{}\n' > /opt/awf/ccusage-neutral.json \
    && chmod 0644 /opt/awf/ccusage-neutral.json

# ── Stage 6: Python tooling the agent may need inside the container ────────
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir \
        "alembic>=1.13" \
        "pytest>=8" \
        "psycopg[binary]>=3.1" \
        "uv>=0.5"

# ── Stage 7: non-root user + workspace mount point ─────────────────────────
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
