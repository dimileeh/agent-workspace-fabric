# Review Thread PRRT_kwDOSJAM6s6CFEMQ Plan

## Problem Statement And Scope

The review reports that append-only workspace events currently reserve
`event_order` values by incrementing `Workspace.version`. Control endpoints use
`Workspace.version` for `If-Match`, so unrelated audit/log/runtime events can
turn a fresh operator version into a false `VERSION_CONFLICT`.

Scope is limited to separating event ordering from optimistic control versioning
for workspace events and updating the direct migration/model/repository/control
tests that encode this behavior.

## Requirements Checklist

- Append-only `add_event` and `add_events` reserve monotonic workspace-local
  `event_order` values without changing `Workspace.version`.
- Actual workspace mutations that already participate in optimistic control
  versioning, such as state transitions and failed-workspace remonitor resets,
  still increment `Workspace.version`.
- PostgreSQL migration behavior preserves event-order assignment for existing
  rows and legacy writers that omit `event_order`.
- Regression tests demonstrate that event-only traffic does not invalidate a
  subsequent control operation using the originally fetched version.
- Existing event-order ordering guarantees remain covered.

## Implementation Steps

1. Add a `Workspace.event_sequence` column and migration handling.
2. Change repository event-order reservation to advance `event_sequence`, with
   an explicit option to also advance `version` for real workspace mutations.
3. Update state transitions and failed remonitor reset paths to bump the control
   version exactly when workspace state/data changes.
4. Update or add focused unit tests for repository, service, API, and migration
   behavior.
5. Run narrow tests first, then lint/type/spec checks as time permits.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents tests/unit/service/test_controls_lifecycle.py tests/unit/api/test_workspace_controls_idempotency.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: the targeted tests and static checks pass, or any unavailable
environment dependency is documented in validation.
