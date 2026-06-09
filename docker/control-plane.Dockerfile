FROM ghcr.io/astral-sh/uv:python3.12-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:${PATH}"

ARG DOCKER_CE_CLI_VERSION=5:29.4.1-1~debian.12~bookworm
ARG DOCKER_COMPOSE_PLUGIN_VERSION=5.1.3-1~debian.12~bookworm
# Buildx is the client-side plugin Compose uses to drive BuildKit builds.
# The control plane runs `docker compose up` for the outer workspace stack
# (which builds managed companions), so it needs buildx to avoid the legacy
# builder and the "requires buildx plugin" warning.
ARG DOCKER_BUILDX_PLUGIN_VERSION=0.34.1-1~debian.12~bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        openssh-client \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        "docker-ce-cli=${DOCKER_CE_CLI_VERSION}" \
        "docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}" \
        "docker-buildx-plugin=${DOCKER_BUILDX_PLUGIN_VERSION}" \
        gh \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version \
    && docker compose version \
    && docker buildx version

WORKDIR /app

COPY pyproject.toml uv.lock README.md alembic.ini .env.example compose.yaml openapi.json ./
COPY docs ./docs
COPY migrations ./migrations
COPY docker/agent-runtime.Dockerfile docker/control-plane.Dockerfile ./docker/
COPY docker/compose/local-service.yml ./docker/compose/local-service.yml
COPY docker/compose/workspace.base.yml.j2 ./docker/compose/workspace.base.yml.j2
COPY apps/console/Dockerfile apps/console/next.config.ts apps/console/package-lock.json apps/console/package.json apps/console/postcss.config.mjs apps/console/tsconfig.json ./apps/console/
COPY apps/console/app ./apps/console/app
COPY apps/console/components ./apps/console/components
COPY apps/console/lib ./apps/console/lib
COPY apps/console/scripts ./apps/console/scripts
COPY src ./src

RUN uv sync --frozen --extra dev
