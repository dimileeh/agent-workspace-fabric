# Workspace Event Order Backfill Validation

Plan reference: `plans/workspace_event_order_backfill_PLAN.md`

## Requirement Status

- Complete: Existing `workspace_events` rows receive a deterministic
  per-workspace `event_order` during upgrade.
- Complete: Ordering is stable for same-workspace rows and uses existing event
  chronology plus deterministic `id` ordering for same-timestamp ties.
- Complete: Regression coverage proves an upgraded database does not leave
  pre-existing rows unordered.
- Complete: Existing Alembic head/schema checks continue to pass.

## Evidence

- Changed `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py` to
  backfill `event_order` with a per-workspace `row_number()` before creating
  the supporting index.
- Changed `tests/unit/db/test_migration_graph.py` with an upgrade regression
  that seeds rows at revision `d6e7f8a9b0c1` and verifies ordered rows after
  upgrading to head.
- Confirmed the new regression failed before the migration change because
  `event_order` stayed `NULL`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py tests/unit/db/test_migration_graph.py`
  passed.

## Gaps

None.
