# Review Thread PRRT_kwDOSJAM6s6CEXi Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CEXi_` reports that the
`workspace_event_order` migration can leave `workspace_events.event_order` null
for rows inserted by old API/worker processes during the documented bootstrap
flow. The migration's DDL transaction blocks writers during the historical
backfill, but the concurrent index block commits before the bootstrap recreates
API/worker services, so old writers can still insert null event orders after the
backfill has completed.

Scope is limited to preserving workspace event ordering during this migration
and adding regression coverage for old writers that do not provide
`event_order`.

## Requirements Checklist

- Add a failing regression test that simulates an old writer inserting a
  `workspace_events` row without `event_order` after upgrading through the
  migration.
- Update the migration so inserts that omit `event_order` after the migration
  still receive a workspace-local ordering key and advance `workspaces.version`.
- Keep existing backfill behavior for historical rows and existing index
  creation behavior intact.
- Preserve downgrade behavior by removing any migration-added database objects.
- Validate with the narrow migration tests and static checks relevant to the
  touched files.

## Implementation Steps

1. Add a migration regression in `tests/unit/db/test_migration_graph.py` that
   upgrades to `head`, inserts a post-migration event with no `event_order`, and
   asserts that the row receives the next order and the workspace version
   advances.
2. Run the new test before implementation to confirm it fails against the
   current migration.
3. Add a PostgreSQL trigger function and trigger in
   `migrations/versions/e8f9a0b1c2d3_workspace_event_order.py` that fills
   missing event orders by atomically incrementing the owning workspace version.
4. Update downgrade to drop the trigger and function before dropping the column.
5. Run the targeted migration tests and lint/type checks as practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check migrations/versions/e8f9a0b1c2d3_workspace_event_order.py tests/unit/db/test_migration_graph.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf` passes, or any unrelated
  pre-existing failure is documented.
