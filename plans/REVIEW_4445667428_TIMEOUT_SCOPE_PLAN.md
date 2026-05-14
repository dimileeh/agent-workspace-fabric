# Review 4445667428 Timeout Scope Plan

## Problem Statement And Scope

Review comment `issue:4445667428` reports that the
`e8f9a0b1c2d3_workspace_event_order` migration keeps
`lock_timeout = '5s'` active across both the `ALTER TABLE ... ADD COLUMN` DDL
and the full-table event-order backfill DML. The short timeout is appropriate
for deploy-facing DDL lock acquisition, but it can make the row-by-row backfill
fail behind ordinary concurrent writers.

This plan addresses only the remaining timeout-scope concern. The remonitor
`state_reset` event-order reservation and bounded secondary-history overlap
comments are already covered by the current code and regression tests.

## Requirements Checklist

- Keep the short `lock_timeout` around the event-order column DDL.
- Disable the short `lock_timeout` before the backfill and workspace-version
  synchronization DML while retaining the migration `statement_timeout`.
- Preserve concurrent-index timeout guardrails in the autocommit block.
- Add or update a regression test that verifies the timeout scope ordering.
- Run focused migration tests and lint for the changed files.
- Commit the scoped fix locally without branch changes or pushes.

## Implementation Steps

1. Update `test_workspace_event_order_migration_has_timeout_guardrails` to
   assert that the migration disables the local lock timeout after the column
   DDL and before the `workspace_events` backfill.
2. Run that focused test to confirm it fails against the current migration.
3. Patch `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py` to issue
   `SET LOCAL lock_timeout = '0'` after the column DDL, with a concise rationale.
4. Re-run the focused guardrail test and the existing migration rerun/backfill
   tests.
5. Run ruff on the changed migration test and migration file.
6. Record validation evidence in
   `plans/REVIEW_4445667428_TIMEOUT_SCOPE_VALIDATION.md`, then commit.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails -q`
  fails before the migration patch and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_reruns_after_column_exists tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events -q`
  passes after the migration patch.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py tests/unit/db/test_migration_graph.py`
  passes.
