# Review 4445667428 Failure Causality Plan

## Problem Statement And Scope

Address the Greptile review-level comment on PR #242 for failure-causality event
ordering and migration safety. The scope is limited to the cited failure
history, workspace event-order reservation, migration index creation, and
targeted regression coverage.

## Requirements Checklist

- Preserve secondary failure history without duplicating re-extracted truncated
  embedded histories.
- Route `transition_if_current` event-order assignment through the shared
  workspace event-order reservation path instead of an inline version bump.
- Build the new workspace event ordering index concurrently in the migration.
- Add targeted coverage for same-tick ordering between synthetic
  `workspace.secondary_failure_recorded` events and later real
  `workspace.state_changed` failure events.
- Keep changes scoped and preserve AWF branch/push policy.

## Implementation Steps

1. Add failing regression tests for secondary-history deduplication,
   `transition_if_current` reservation helper usage, and concurrent index DDL.
2. Update `failure_causality.py` to merge embedded secondary histories using a
   more robust sequence-overlap rule.
3. Refactor `WorkspaceRepository.transition_if_current` to lock the matching row,
   reserve an event order through `_reserve_workspace_event_orders`, then append
   the transition event with that reserved order.
4. Update the event-order migration to use Alembic autocommit blocks and
   PostgreSQL concurrent index create/drop.
5. Add or complete same-tick synthetic-vs-state failure event coverage.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails -q`
- Broader targeted pass if time allows:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py tests/unit/db/test_workspace_repository.py::TestAddEvents tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails -q`

Pass criteria: the new regressions fail before implementation where practical,
then pass after the scoped fixes without unrelated file changes.
