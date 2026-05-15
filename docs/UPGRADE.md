# Upgrade Guide

Use this guide when moving an existing local AWF checkout or installed CLI to a
newer version.

## Local CLI Install

For a released install:

```bash
uv tool upgrade aira-awf
```

For a source checkout:

```bash
git pull
uv tool install . --force
```

## Local Service Stack

After upgrading the CLI or source checkout, rebuild and verify the local stack:

```bash
awf service bootstrap --timeout-seconds 300
awf service status --format pretty
awf smoke run --mocked-local --format pretty
```

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
