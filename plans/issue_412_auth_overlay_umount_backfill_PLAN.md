# Issue #412 Auth Overlay Umount Backfill Plan

## Problem Statement And Scope

Pre-upgrade `workspace.terminal_runtime_released` rows can record
`auth_overlay_unmounted: false` without the retry marker introduced later by #399/#410.
The runtime retry scan only finds marker events, so those historical failures never enter
the normal deferred retry path. This task adds a focused one-time backfill for those rows
without changing the runtime scan or adding JSON-value predicates there.

This plan executes the saved AWF implementation contract in
`docs/awf-plans/ws_15d53bef87104117ab3df405.md`.

## Requirements Checklist

- Add an idempotent Alembic data migration after `f9a0b1c2d3e4`.
- Seed `workspace.terminal_auth_overlay_unmount_pending` markers only for latest effective
  `workspace.terminal_runtime_released` rows whose payload explicitly has
  `auth_overlay_unmounted: false`.
- Leave rows with an existing current-cycle `pending`, `resolved`, or `exhausted` marker
  untouched.
- Preserve the existing `TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING` reason code and payload
  shape.
- Keep the runtime retry scan dialect-portable and unchanged; JSON parsing belongs in the
  migration helper.
- Cover backfilled retry, existing-marker no-op, idempotence, non-qualifying filters, and
  SQLite-compatible helper behavior with focused tests.

## Implementation Steps

1. Add failing tests for the migration helper and retry integration.
2. Implement the Alembic migration with a reusable helper that reads candidate payloads,
   reserves workspace-local `event_order` values, and inserts deterministic pending events.
3. Update the Alembic head expectation.
4. Run focused pytest and static checks for changed files only; broad coverage remains owned
   by AWF/GitHub after agent completion.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q -k "auth_overlay or alembic_revision_graph_has_single_head"`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k backfill`
- `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`
- `uv run --python 3.12 --extra dev mypy migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py`
