# Upgrade Guide

Use the upgrade path that matches the first-run lane or install path you chose.
After upgrading, run `awf start` and mocked smoke against the project
initialized during first run to refresh local Core and prove the operator path
still works. Replace `<path>` below with that project path. The hosted curl
installer lane is intentionally omitted until its public installer, manifest,
checksums, and distribution artifacts are published and verified.

## uv tool

The `uv tool` lane is release-installed and package-manager mediated:

```bash
uv tool upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## pipx

The `pipx` lane is release-installed and package-manager mediated:

```bash
pipx upgrade agent-workspace-fabric
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
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## Source Checkout With Global Tool Install

This lane uses an inspectable source checkout and a global tool installed from
that checkout:

```bash
cd /path/to/aira-agent-workspace-fabric
git pull
uv tool install . --force
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

## Source Checkout With No Global Install

This lane uses an inspectable source checkout and no global install. Keep
running AWF through `uv run` from the checkout:

```bash
cd /path/to/aira-agent-workspace-fabric
git pull
uv sync --extra dev
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

For lanes that put `awf` on `PATH`:

```bash
awf start
awf service status --format pretty
awf smoke run --project <path> --mocked-local --format pretty
```

For the source checkout with no global install lane, run AWF from the checkout:

```bash
cd /path/to/aira-agent-workspace-fabric
uv sync --extra dev
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
