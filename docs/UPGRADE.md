# Upgrade Guide

Use the upgrade path that matches the first-run lane you chose. After upgrading,
run `awf start` and mocked smoke to refresh local Core and prove the operator
path still works.

## Curl Installer

The curl installer lane is release-installed. Upgrade by rerunning the same
installer after the release manifest and checksum-backed artifacts are
published:

```bash
curl -fsSL https://aira.pro/install.sh | sh
awf start
awf service status --format pretty
awf smoke run --mocked-local --format pretty
```

Use `--version` only when you intentionally want to pin a specific published
release:

```bash
curl -fsSL https://aira.pro/install.sh | sh -s -- --version 0.1.0
```

## uv tool

The `uv tool` lane is release-installed and package-manager mediated:

```bash
uv tool upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --mocked-local --format pretty
```

## pipx

The `pipx` lane is release-installed and package-manager mediated:

```bash
pipx upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --mocked-local --format pretty
```

## Source Checkout With Global Tool Install

This lane uses an inspectable source checkout and a global tool installed from
that checkout:

```bash
cd /path/to/aira-agent-workspace-fabric
git pull
uv tool install . --force
awf start --source-checkout "$PWD"
awf service status --format pretty
awf smoke run --mocked-local --format pretty
```

## Source Checkout With No Global Install

This lane uses an inspectable source checkout and no global install. Keep
running AWF through `uv run` from the checkout:

```bash
cd /path/to/aira-agent-workspace-fabric
git pull
uv sync --extra dev
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf smoke run --mocked-local --format pretty
```

## Rollback

If a local upgrade blocks development:

1. Return to the previous Git revision or reinstall the previous package
   version for your lane.
2. Run `awf start` from that lane.
3. Check `awf service status --format pretty`.
4. Run `awf smoke run --mocked-local --format pretty`.

`awf start` is designed to be idempotent for local development. Do not delete
`.awf` state or Docker volumes unless a specific troubleshooting step requires
it.

Homebrew is planned after stable tagged artifacts and formula audit coverage;
there is no supported `brew` upgrade path yet.
