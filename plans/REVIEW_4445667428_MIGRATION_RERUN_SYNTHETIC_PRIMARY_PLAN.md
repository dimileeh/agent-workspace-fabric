# Review 4445667428 Migration Rerun And Synthetic Primary Plan

## Problem Statement And Scope

Address the remaining review-level observations from PR comment
`issue:4445667428`:

- The `workspace_events.event_order` migration should tolerate reruns after a
  partially advanced deploy, especially because the concurrent index step runs
  outside the transactional backfill.
- `workspace.secondary_failure_recorded` events can intentionally be the
  selected causality `primary_event` when they carry embedded primary failure
  evidence, and that operator-facing behavior should be explicit.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Add a regression test that simulates the `event_order` column already
  existing before the migration is rerun and proves Alembic can still upgrade
  to head.
- Make the migration idempotent for the event-order column, backfill, version
  advancement, index creation, and downgrade cleanup.
- Add regression coverage documenting that a secondary-failure-recorded event
  with embedded primary evidence may be returned as the current-epoch primary
  event.
- Add a concise code comment/docstring explaining why that synthetic event
  source is expected.
- Run the narrow tests and lint for touched files.
- Commit the local fix with a conventional commit message referencing
  `4445667428`.
- Emit the required `AWF-VERDICT` line when complete.

## Implementation Steps

1. Add the migration rerun regression in `tests/unit/db/test_migration_graph.py`.
2. Add the synthetic primary-event regression in
   `tests/unit/service/test_failure_causality.py`.
3. Run the new tests and confirm they fail where practical.
4. Update `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py` to use
   PostgreSQL `IF NOT EXISTS`/`IF EXISTS` DDL and only backfill missing
   `event_order` values.
5. Document the synthetic secondary event source in
   `src/awf/service/failure_causality.py`.
6. Re-run focused verification, then write the validation document.

## Verification Commands And Pass Criteria

- Before implementation, the new migration rerun test should fail on duplicate
  `workspace_events.event_order`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_reruns_after_column_exists tests/unit/service/test_failure_causality.py::test_primary_failure_event_can_be_synthetic_secondary_record -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_backfills_existing_events tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py src/awf/service/failure_causality.py tests/unit/db/test_migration_graph.py tests/unit/service/test_failure_causality.py`
  must pass.
