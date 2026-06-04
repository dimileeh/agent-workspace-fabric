# Unify AWF Local Runtime Configuration Around Root `.env`

## Problem Statement

AWF currently has two local runtime configuration surfaces:

- root `.env` for package/Python flows and some first-run helpers;
- `docker/compose/.env` for source-checkout Docker Compose interpolation.

Because `docker compose -f docker/compose/local-service.yml ...` uses
`docker/compose` as the Compose project directory, service containers can miss
keys that operators placed in the root `.env`. The local runtime should have one
operator-edited env file at the install/source root.

## Scope

- Make root `.env` the canonical local runtime env file.
- Add a root `compose.yaml` public Compose entrypoint that includes the existing
  `docker/compose/local-service.yml` asset.
- Migrate legacy `docker/compose/.env` values into root `.env` without printing
  secret values.
- Update service/bootstrap/setup/start/status/doctor/logs/gc/MCP env-file
  resolution to target root `.env`.
- Extend Compose interpolation discovery through included Compose files.
- Update docs and installer backlog references away from operator editing of
  `docker/compose/.env`.

## Requirements Checklist

- [ ] Root `compose.yaml` supports `docker compose up -d --build` from the repo
  or install root.
- [ ] Source-checkout and packaged service helpers resolve the Compose file from
  the root entrypoint and resolve env files to root `.env`.
- [ ] `awf setup`, `awf start`, `awf service bootstrap/status/doctor/logs/gc`,
  and MCP client registration use root `.env`.
- [ ] Legacy `docker/compose/.env` is a migration source only.
- [ ] Migration creates root `.env` from `.env.example` plus legacy values when
  needed.
- [ ] Migration imports only missing keys when root `.env` exists.
- [ ] Conflicts keep root `.env` canonical and report only key names.
- [ ] Legacy env file is moved to a timestamped backup after migration.
- [ ] No migration output, structured payload, logs, or test assertions expose
  raw secret values.
- [ ] Existing process environment precedence is preserved.
- [ ] Compose interpolation helpers discover `${VAR}` references in included
  Compose files.
- [ ] Docs describe root `.env` as the single operator-edited local runtime env
  file, with `docker/compose/local-service.yml` treated as an internal included
  asset.

## Implementation Steps

1. Add focused tests for:
   - root env path resolution in source checkout and packaged/bootstrap helpers;
   - MCP setup env-file path resolution;
   - legacy migration create/import/conflict/backup/redaction cases;
   - Compose include interpolation key discovery.
2. Add `compose.yaml` at the repository root and include it in packaged
   bootstrap assets.
3. Update service config constants and CLI/bootstrap resolution helpers so the
   canonical env file is root `.env`.
4. Add a legacy env migration helper with redacted structured results, and call
   it from setup/start/bootstrap entrypoints before service env is used.
5. Teach Compose interpolation helpers to recursively inspect Compose includes
   while avoiding cycles.
6. Update docs and installer backlog references to use root `.env` and raw
   `docker compose up -d --build`.
7. Run focused validation and write the validation report.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest <focused env/bootstrap/cli tests> -q`
- `uv run --python 3.12 --extra dev ruff check <touched Python files>`
- `uv run --python 3.12 --extra dev mypy <touched source Python files>`
- `docker compose config` from repo root with redacted/test env where practical.

## Pass Criteria

- Targeted tests for env resolution, migration, and Compose include parsing pass.
- Lint/type checks pass on touched Python files.
- Root Compose config resolves from root and does not require
  `docker/compose/.env`.
- Documentation no longer tells source contributors/operators to edit
  `docker/compose/.env` as active runtime configuration.
