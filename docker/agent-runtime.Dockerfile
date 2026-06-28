# AWF agent-runtime image — the container that holds the repo worktree and
# the coding CLIs (Codex, Claude Code, Cursor, Gemini, OpenCode, Grok). Built multi-arch
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

# ── Stage 2: Docker CLI + Compose + Buildx plugins ────────────────────────
# Buildx is the client-side plugin Compose uses to drive BuildKit builds.
# Without it, `docker compose` warns ("requires buildx plugin to be
# installed") and falls back to the legacy builder. The agent is the Docker
# client inside DinD workspaces (DOCKER_HOST=tcp://docker:2375), so it needs
# buildx locally even though the DinD daemon already ships BuildKit.
ARG DOCKER_CE_CLI_VERSION=5:29.4.1-1~debian.12~bookworm
ARG DOCKER_COMPOSE_PLUGIN_VERSION=5.1.3-1~debian.12~bookworm
ARG DOCKER_BUILDX_PLUGIN_VERSION=0.34.1-1~debian.12~bookworm
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
        "docker-buildx-plugin=${DOCKER_BUILDX_PLUGIN_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version \
    && docker compose version \
    && docker buildx version

# ── Stage 3: GitHub CLI ───────────────────────────────────────────────────
ARG GH_VERSION=2.92.0
ARG GH_AMD64_SHA256=8f8212b1a9cec261a8839e0893168f50d3fc70f095da257feef4229234cefdf8
ARG GH_ARM64_SHA256=34d620b7c884774ed86236541535170889fda0b99aafbdab8b69c7d458b5ca6b
RUN set -eux; \
    gh_arch="$(dpkg --print-architecture)"; \
    gh_asset="gh_${GH_VERSION}_linux_${gh_arch}.deb"; \
    case "$gh_arch" in \
      amd64) expected_hash="${GH_AMD64_SHA256}" ;; \
      arm64) expected_hash="${GH_ARM64_SHA256}" ;; \
      *) echo "Unsupported GitHub CLI architecture: $gh_arch" >&2; exit 1 ;; \
    esac; \
    if [ -z "$expected_hash" ]; then \
      echo "GitHub CLI checksum is not pinned for ${gh_asset}" >&2; \
      exit 1; \
    fi; \
    gh_deb="/tmp/${gh_asset}"; \
    trap 'rm -f "$gh_deb"' EXIT; \
    curl --fail --show-error --location --silent \
      --retry 5 \
      --retry-delay 2 \
      --retry-all-errors \
      --connect-timeout 20 \
      --max-time 300 \
      -o "$gh_deb" \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${gh_asset}"; \
    actual_hash="$(sha256sum "$gh_deb")"; \
    actual_hash="${actual_hash%% *}"; \
    if [ "$actual_hash" != "$expected_hash" ]; then \
      echo "GitHub CLI checksum mismatch for ${gh_asset}: expected ${expected_hash}, got ${actual_hash}" >&2; \
      exit 1; \
    fi; \
    apt-get install -y --no-install-recommends "$gh_deb"; \
    gh --version

# ── Stage 4: Node.js (for npm-backed coding CLIs) ─────────────────────────
ARG NODE_VERSION
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && ln -sf "$(readlink -f "$(command -v node)")" /usr/local/bin/node \
    && rm -rf /var/lib/apt/lists/* \
    && test -x /usr/local/bin/node \
    && node --version \
    && npm --version

# ── Stage 5: coding CLIs ──────────────────────────────────────────────────
#
# npm-backed CLIs are pinned to a version. Bump via PR so we can verify the
# output format hasn't drifted in the adapters.
ARG CODEX_VERSION=0.130.0
# 2.1.154+ is required for Claude Opus 4.8 (the default model in defaults.py);
# older CLIs reject `--model claude-opus-4-8`. Keep this >= the default model's
# minimum supported CLI.
ARG CLAUDE_CODE_VERSION=2.1.158
ARG GEMINI_VERSION=0.42.0
ARG OPENCODE_VERSION=1.15.2
ARG GROK_VERSION=0.2.14
# Usage collector. Pinned (not fetched via runtime npx/bunx) so AWF's
# per-workspace usage sampler reads local provider usage files offline.
ARG CCUSAGE_VERSION=20.0.3

# Cursor CLI tracks the official installer because Cursor does not document a
# stable standalone version pin for the Linux installer.
RUN set -eux; \
    install -d -m 0755 /opt/cursor; \
    curl https://cursor.com/install -fsS | HOME=/opt/cursor bash; \
    cursor_path="/opt/cursor/.local/bin/cursor-agent"; \
    if [ ! -e "$cursor_path" ]; then \
      echo "cursor-agent was not installed by the Cursor installer" >&2; \
      exit 1; \
    fi; \
    ln -sf "$cursor_path" /usr/local/bin/cursor-agent; \
    chmod -R a+rX /opt/cursor; \
    chmod +x /usr/local/bin/cursor-agent; \
    test -x /usr/local/bin/cursor-agent

RUN set -eux; \
    max_attempts=3; \
    attempt=1; \
    while [ "$attempt" -le "$max_attempts" ]; do \
      if npm install -g --no-fund --no-audit \
        @openai/codex@${CODEX_VERSION} \
        @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
        @google/gemini-cli@${GEMINI_VERSION} \
        opencode-ai@${OPENCODE_VERSION} \
        @xai-official/grok@${GROK_VERSION} \
        ccusage@${CCUSAGE_VERSION}; then \
        break; \
      fi; \
      echo "npm install attempt $attempt/$max_attempts failed, retrying in 10s..." >&2; \
      if [ "$attempt" -eq "$max_attempts" ]; then \
        echo "npm install failed after $max_attempts attempts" >&2; \
        exit 1; \
      fi; \
      sleep 10; \
      attempt=$((attempt + 1)); \
    done; \
    npm cache clean --force; \
    codex --version || true; \
    claude --version || true; \
    cursor-agent --version || true; \
    gemini --version || true; \
    opencode --version || true; \
    grok --version || true; \
    ccusage --version

# Gemini CLI 0.42.0 only enables its ripgrep-backed search tool when a bundled
# platform-specific rg binary exists; it does not fall back to the system rg on
# PATH. Link the Debian ripgrep package into the bundled layout so Gemini uses
# its file-aware RipGrepTool instead of the directory-only GrepTool fallback.
RUN set -eux; \
    gemini_entrypoint="$(readlink -f "$(command -v gemini)")"; \
    gemini_bundle_dir="$(dirname "$gemini_entrypoint")"; \
    rg_platform="$(node -p 'process.platform')"; \
    rg_arch="$(node -p 'process.arch')"; \
    mkdir -p "$gemini_bundle_dir/vendor/ripgrep"; \
    ln -sf "$(command -v rg)" "$gemini_bundle_dir/vendor/ripgrep/rg-${rg_platform}-${rg_arch}"; \
    test -x "$gemini_bundle_dir/vendor/ripgrep/rg-${rg_platform}-${rg_arch}"

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
    && mkdir -p /workspace /home/agent/.config/cursor \
    && chown -R agent:agent /workspace /home/agent/.config

USER agent
WORKDIR /workspace

RUN set -eux; \
    command -v cursor-agent; \
    test -x /usr/local/bin/cursor-agent; \
    cursor-agent --version

# tini reaps zombies when the CLI forks subprocesses (common in test runs).
ENTRYPOINT ["/usr/bin/tini", "--"]
# Default command keeps the container alive so ``docker compose exec`` can run
# coding CLIs inside. The adapter layer owns the actual invocations.
CMD ["sh", "-c", "sleep infinity"]
