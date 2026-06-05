# Issue #412 Auth Overlay Umount Backfill Validation

Plan reference: `plans/issue_412_auth_overlay_umount_backfill_PLAN.md`

## Requirement Status

- Complete: Added an idempotent Alembic data migration after `f9a0b1c2d3e4`.
- Complete: Backfilled only latest effective `workspace.terminal_runtime_released` rows whose
  payload explicitly records `auth_overlay_unmounted: false`.
- Complete: Rows with existing current-cycle `pending`, `resolved`, or `exhausted` markers are
  skipped and their marker payload/order remain untouched.
- Complete: Backfilled markers use `workspace.terminal_auth_overlay_unmount_pending`,
  `TERMINAL_AUTH_OVERLAY_UNMOUNT_PENDING`, and payload shape
  `compose_project_name` / `workspace_status` / `attempt: 1`.
- Complete: Runtime retry scan remains unchanged; JSON payload interpretation is isolated to the
  migration helper and is exercised on Postgres and SQLite.
- Complete: Focused tests cover backfilled retry, existing-marker no-op, idempotence,
  non-qualifying filters, and SQLite-compatible payload parsing.

## Files Changed

- `migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py`
- `tests/unit/db/test_migration_graph.py`
- `tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`
- `plans/issue_412_auth_overlay_umount_backfill_PLAN.md`
- `plans/issue_412_auth_overlay_umount_backfill_VALIDATION.md`

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q -k "auth_overlay or alembic_revision_graph_has_single_head"`: passed, 3 passed / 6 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q -k alembic_upgrade_head_creates_scheduler_record_tables`: passed, 1 passed / 8 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k backfill`: passed, 1 passed / 23 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q`: passed, 24 passed.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`: passed.
- `uv run --python 3.12 --extra dev mypy migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py`: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker/cleanup.py src/awf/control/worker/cleanup_auth_overlay.py`: passed.

Full AWF/GitHub validation, including the hard coverage gate, was not run inside the agent phase
per the AWF workspace contract.
