# Address Thread PRRT_kwDOSJAM6s6HWUXQ Plan

## Problem Statement and Scope

The auth-overlay unmount backfill migration sets a short PostgreSQL
`lock_timeout` before running its data backfill. The backfill reserves
workspace event order with per-workspace `UPDATE workspaces ... RETURNING`
DML, so the short lock wait can abort on ordinary live writer row locks.

Scope is limited to the migration timeout behavior and a focused regression
test for that guardrail.

## Requirements Checklist

- Verify the review claim against the migration code.
- Add focused test coverage showing the short lock timeout is cleared before
  `backfill_auth_overlay_unmount_pending(bind)` runs.
- Keep `statement_timeout` active so total PostgreSQL migration runtime remains
  bounded.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks.
- Commit the thread fix locally without pushing.

## Implementation Steps

1. Add a focused migration-text regression test in `tests/unit/db/test_migration_graph.py`.
2. Confirm the new test fails against the current migration.
3. Reset PostgreSQL `lock_timeout` before invoking the backfill.
4. Run the targeted test and focused lint for changed Python files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_auth_overlay_unmount_backfill_resets_lock_timeout_before_dml -q`
  passes after failing before the migration fix.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/0f1e2d3c4b5a_backfill_auth_overlay_unmount_pending.py tests/unit/db/test_migration_graph.py`
  passes.
- Full AWF/GitHub validation is left to AWF after agent completion.
