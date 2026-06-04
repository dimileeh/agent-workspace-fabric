# PR403 Legacy Env Migration Guard Plan

## Problem

A new PR review thread found that legacy `docker/compose/.env` migration can run
from arbitrary project directories when AWF falls back to `Path(".env")` without
a verified source checkout. That can import project-specific compose secrets
into `$PWD/.env` and move the project file aside as a backup.

## Plan

- Gate `_migrate_legacy_service_env_file()` so it only migrates when the
  canonical `.env` belongs to an AWF source-shaped root.
- Preserve migration behavior for verified/source-root local AWF checkouts.
- Add/adjust CLI tests:
  - source-root bootstrap still migrates legacy AWF compose env,
  - non-source current project does not migrate and leaves
    `docker/compose/.env` intact,
  - init bootstrap migration and state-dir tests write explicit AWF source
    markers when they intentionally model a source checkout.
- Validate focused env migration and service/init CLI tests, then rerun shard 2
  if practical before commit.
