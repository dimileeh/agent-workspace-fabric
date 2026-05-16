# Review 4445667428 Timeout Scope Validation

Plan reference: `plans/REVIEW_4445667428_TIMEOUT_SCOPE_PLAN.md`

## Requirement Status

- Keep the short `lock_timeout` around the event-order column DDL: Complete.
  - The migration still sets `SET LOCAL lock_timeout = '5s'` before
    `ALTER TABLE workspace_events ADD COLUMN IF NOT EXISTS event_order INTEGER`.
- Disable the short `lock_timeout` before the backfill and workspace-version
  synchronization DML while retaining the migration `statement_timeout`:
  Complete.
  - The migration now issues `SET LOCAL lock_timeout = '0'` after the column DDL
    and before the `UPDATE workspace_events` backfill.
  - `SET LOCAL statement_timeout = '10min'` remains in place for the migration
    transaction.
- Preserve concurrent-index timeout guardrails in the autocommit block:
  Complete.
  - The concurrent index block still sets and resets `lock_timeout` and
    `statement_timeout`.
- Add or update a regression test that verifies the timeout scope ordering:
  Complete.
  - Updated
    `test_workspace_event_order_migration_has_timeout_guardrails` to assert the
    ordering `SET LOCAL lock_timeout = '5s'` -> `ADD COLUMN` ->
    `SET LOCAL lock_timeout = '0'` -> `UPDATE workspace_events`.
- Run focused migration tests and lint for the changed files: Complete.
- Commit the scoped fix locally without branch changes or pushes: Complete.
  - The scoped files are included in the local fix commit for this review
    comment.

## Evidence

Files changed:

- `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py`
- `tests/unit/db/test_migration_graph.py`
- `plans/REVIEW_4445667428_TIMEOUT_SCOPE_PLAN.md`
- `plans/REVIEW_4445667428_TIMEOUT_SCOPE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails -q`
  - Failed before the migration patch with `ValueError: substring not found` for
    `SET LOCAL lock_timeout = '0'`, confirming the regression test detected the
    reviewer concern.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails -q`
  - Passed after the migration patch: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_reruns_after_column_exists tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events -q`
  - Passed: 2 passed.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py tests/unit/db/test_migration_graph.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle.py::test_remonitor_failed_workspace_reserves_state_reset_event_order -q`
  - Passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_dedupes_truncated_secondary_history_windows -q`
  - Passed: 1 passed.

## Gaps

No implementation gaps remain.
