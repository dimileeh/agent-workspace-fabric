# Review 4445667428 Event Order Backfill Plan

## Problem Statement and Scope

Greptile's review-level comment identifies two event ordering risks in the
failure causality work:

- Secondary failure history orders same-timestamp legacy `NULL` event_order rows
  before ordered rows, while latest-failed-event selection treats `NULL` as lower
  priority.
- The `workspace_events.event_order` backfill can assign an order greater than
  the current `workspaces.version`, causing the next post-migration transition
  to reuse an existing order.

Scope is limited to preserving deterministic event ordering for the migration
and failure causality history reconstruction. No branch, push, PR-comment, or
broad repository behavior changes are in scope.

## Requirements Checklist

- Add a regression test showing the migration leaves each workspace version at
  least as high as its maximum backfilled event order.
- Add a regression test showing secondary failure history sorts same-timestamp
  ordered events before legacy `NULL` event_order events.
- Update the migration so future transition-created event orders continue after
  the backfilled event-order range.
- Update `_secondary_failure_history_for_current_epoch` to use `NULLS LAST` for
  ascending event_order sorting.
- Run the narrowest relevant tests for the migration and failure causality
  changes.
- Commit only the files changed for this review comment.

## Implementation Steps

1. Update tests first and confirm the new tests fail against current code.
2. Change the migration to bump `workspaces.version` to the maximum backfilled
   `workspace_events.event_order` where needed.
3. Change secondary history ordering to `event_order.asc().nullslast()`.
4. Re-run targeted tests, then run focused lint for changed Python files.
5. Record validation results in
   `plans/REVIEW_4445667428_EVENT_ORDER_BACKFILL_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_orders_same_timestamp_secondary_history_null_event_orders_last -q`
- `uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py src/awf/service/failure_causality.py tests/unit/db/test_migration_graph.py tests/unit/service/test_failure_causality.py`

Both commands must pass.
