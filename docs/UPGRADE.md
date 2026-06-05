# Upgrade Guide

Use this guide when moving an existing local AWF checkout or installed CLI to a
newer version.

## Local CLI Install

For a released install:

```bash
uv tool upgrade agent-workspace-fabric
```

For `pipx` installs:

```bash
pipx upgrade agent-workspace-fabric
```

For virtualenv installs (activate your existing venv first):

```bash
. .venv/bin/activate
pip install --upgrade agent-workspace-fabric
```

For a source checkout:

```bash
git pull
uv tool install . --force
```

Homebrew is planned after stable tagged artifacts and formula audit coverage;
there is no supported `brew` upgrade path yet.

## Local Service Stack

After upgrading the CLI or source checkout, rebuild and verify the local stack:

```bash
awf service bootstrap --timeout-seconds 300
awf service status --format pretty
awf smoke run --mocked-local --format pretty
```

Before upgrading a service stack with live control-plane data, follow the
[Local Service Upgrade](CONCEPTS.md#local-service-upgrade) runbook. It captures a
pre-upgrade Postgres backup, runs migrations through `awf service bootstrap`,
and points migration failures at the migrate logs before changing volumes or
state. Treat rollback after data migrations as a restore from that backup, as
covered in [Local Service Rollback](CONCEPTS.md#local-service-rollback).

Use `awf service readiness --format pretty` or
`awf service release-readiness --format pretty` only when you need the Core
release gate. That gate checks historical PRD SLO evidence and can fail even
when local health is green.

## Rollback

If a local upgrade blocks development:

1. Return to the previous Git revision or reinstall the previous package
   version.
2. Run `awf service bootstrap --timeout-seconds 300`.
3. Check `awf service status --format pretty`.

AWF service bootstrap is designed to be idempotent for local development. Do not
delete `.awf` state or Docker volumes unless a specific troubleshooting step
requires it.
