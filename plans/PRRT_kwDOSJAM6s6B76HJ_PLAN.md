# PRRT_kwDOSJAM6s6B76HJ Plan

## Problem Statement And Scope

The failure causality loader currently orders same-timestamp failed
`WorkspaceEvent` rows with `WorkspaceEvent.id.desc()`. Event IDs are random
UUID-style strings, so that tie-breaker can select stale preserved-primary
payloads instead of the actual newest failed state-change event.

Scope is limited to making failure-causality event ordering chronological for
same-timestamp workspace events and proving the regression with unit coverage.

## Requirements Checklist

- Add a regression test for same-`occurred_at` failed state-change events where
  UUID order disagrees with chronological event order.
- Persist a workspace-local monotonic event ordering key for events that advance
  workspace lifecycle state or reset failure epochs.
- Update failure causality queries and same-timestamp epoch comparisons to use
  the monotonic ordering key when it is available.
- Preserve the existing conservative behavior for legacy rows that do not have
  the ordering key.
- Keep changes scoped to failure causality, event persistence metadata, and the
  required migration.

## Implementation Steps

1. Add the failing unit regression in `tests/unit/service/test_failure_causality.py`.
2. Add an `event_order` column to `WorkspaceEvent` and the Alembic migration
   graph.
3. Populate `event_order` from `Workspace.version` for creation, transitions,
   conditional transitions, and remonitor state-reset events.
4. Replace failure-causality `id.desc()` tie-breakers with chronological
   `event_order` tie-breakers and refine same-timestamp reset comparisons.
5. Run targeted tests, then lint/type-check the touched Python surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_failure_causality.py tests/unit/db/test_migration_graph.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
