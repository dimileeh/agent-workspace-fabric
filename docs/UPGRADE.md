# Upgrade Guide

Use the upgrade path that matches the first-run lane or install path you chose.
After upgrading, run `awf start` and mocked smoke against the project
initialized during first run to refresh local Core and prove the operator path
still works. Replace `<path>` below with that project path. The hosted curl
installer lane is intentionally omitted until its public installer, manifest,
checksums, and distribution artifacts are published and verified.

Package and virtualenv lanes read `.env` from the current directory when it
exists. If `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD` are not already persisted
there, restore them in the upgrade shell before `awf start`. Restore the same
`AWF_API_TOKEN` used by the running local Core; do not generate a replacement
token during upgrade.

## uv tool

The `uv tool` lane is release-installed and package-manager mediated:

```bash
uv tool upgrade agent-workspace-fabric
if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then
  export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
fi
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## pipx

The `pipx` lane is release-installed and package-manager mediated:

```bash
pipx upgrade agent-workspace-fabric
if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then
  export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
fi
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## Virtualenv / pip

Use this path only when you installed AWF into an active virtualenv with
`pip install agent-workspace-fabric` instead of an isolated tool manager:

```bash
cd /path/to/project-or-env
. .venv/bin/activate
pip install --upgrade agent-workspace-fabric
if ! grep -q '^AWF_API_TOKEN=.' .env 2>/dev/null; then
  : "${AWF_API_TOKEN:?restore the AWF_API_TOKEN used for the running local Core or persist it in .env before upgrading}"
  export AWF_API_TOKEN
fi
if ! grep -q '^AWF_POSTGRES_PASSWORD=.' .env 2>/dev/null; then
  export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
fi
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## Source Checkout With Global Tool Install

This lane uses an inspectable source checkout and a global tool installed from
that checkout. Stop local Core before refreshing source-checkout metadata;
`awf setup` checks the API and Postgres host ports and blocks while the previous
Core stack still holds them:

```bash
cd /path/to/aira-agent-workspace-fabric
git pull
uv tool install . --force
export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## Source Checkout With No Global Install

This lane uses an inspectable source checkout and no global install. Keep
running AWF through `uv run` from the checkout. Stop local Core before
refreshing source-checkout metadata; `awf setup` checks the API and Postgres
host ports and blocks while the previous Core stack still holds them:

```bash
cd /path/to/aira-agent-workspace-fabric
git pull
uv sync --extra dev
export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf smoke run --project <path> --mocked-local --format pretty
```

## Rollback

If a local upgrade blocks development:

1. Return to the previous Git revision or reinstall the previous package
   version for your lane.
2. Run the rollback commands that match your lane.

For release-installed lanes, and for virtualenv/pip installs after activating
the restored environment:

```bash
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

For the source checkout with global tool install lane, run from the restored
checkout and refresh persisted source-checkout metadata before starting:

```bash
cd /path/to/aira-agent-workspace-fabric
uv tool install . --force
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

For the source checkout with no global install lane, run AWF from the checkout:

```bash
cd /path/to/aira-agent-workspace-fabric
uv sync --extra dev
if [ -f docker/compose/.env ]; then
  docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml stop
else
  docker compose -f docker/compose/local-service.yml stop
fi
export AWF_API_TOKEN="${AWF_API_TOKEN:-$(openssl rand -hex 32)}"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf smoke run --project <path> --mocked-local --format pretty
```

`awf start` is designed to be idempotent for local development. Do not delete
`.awf` state or Docker volumes unless a specific troubleshooting step requires
it.

Homebrew is planned after stable tagged artifacts and formula audit coverage;
there is no supported `brew` upgrade path yet.
