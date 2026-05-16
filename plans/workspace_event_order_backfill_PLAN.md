# Workspace Event Order Backfill Plan

## Problem Statement

PR thread `PRRT_kwDOSJAM6s6B9-MR` reports that migration
`e8f9a0b1c2d3_workspace_event_order.py` adds `workspace_events.event_order`
but leaves pre-existing rows as `NULL`. Failure causality now uses
`event_order` as the same-timestamp tiebreaker, so upgraded databases need a
deterministic order for existing workspace events before the application
depends on the column.

## Scope

- Update only the workspace event order migration and directly relevant tests.
- Preserve the nullable model column because runtime code still handles
  unordered rows defensively.
- Do not change branch state, push, or resolve the GitHub thread directly.

## Requirements

- [ ] Existing `workspace_events` rows receive a deterministic per-workspace
      `event_order` during upgrade.
- [ ] Ordering is stable for same-workspace rows and uses existing event
      chronology plus a deterministic tiebreaker.
- [ ] New regression coverage proves an upgraded database does not leave
      pre-existing rows unordered.
- [ ] Existing Alembic head/schema checks continue to pass.

## Implementation Steps

1. Add a migration regression test that upgrades to `d6e7f8a9b0c1`, inserts
   same-timestamp workspace events, upgrades to head, and asserts deterministic
   per-workspace `event_order` values.
2. Run the new test before the migration change and confirm it fails when
   practical.
3. Backfill `event_order` in `e8f9a0b1c2d3_workspace_event_order.py` with a
   `row_number()` window partitioned by `workspace_id` and ordered by
   `occurred_at, id`.
4. Run the targeted migration test and relevant broader migration graph test.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
- Pass criteria: all tests in the migration graph unit module pass, including
  the new backfill regression.
