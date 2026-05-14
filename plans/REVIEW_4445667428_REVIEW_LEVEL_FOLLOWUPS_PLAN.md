# Review 4445667428 Review-Level Followups Plan

## Problem Statement And Scope

Address the review-level follow-up observations on PR comment
`issue:4445667428`:

- The `workspace_events.event_order` migration rewrites existing event rows
  without operational timeout guardrails.
- `WorkspaceRepository.add_events()` derives `event_order` from
  `workspace.version` but does not reserve new version/order values itself.
- `workspace.secondary_failure_recorded` is a public callback event type, so
  its public callback envelope shape should be explicit and must not expose the
  internal causality payload keys.

No branch changes, pushes, rebases, or GitHub comments are in scope.

## Requirements Checklist

- Add a regression check that the event-order migration carries PostgreSQL
  timeout guardrails, then add those guardrails to the migration.
- Add a repository regression test showing `add_events()` advances
  `workspace.version` and assigns strictly increasing event orders after the
  existing latest order.
- Implement `add_events()` so it serializes event-order allocation with a
  workspace row lock and reserves order values itself.
- Remove or adjust callsite/test manual version increments that were only
  compensating for the old `add_events()` behavior.
- Add callback delivery coverage proving `workspace.secondary_failure_recorded`
  sends the stable sanitized workspace envelope and not internal causality
  payload keys.
- Document the public callback event-type examples and sanitized
  `workspace.secondary_failure_recorded` envelope shape.
- Run focused tests plus lint for touched Python modules.
- Commit the local fix with a conventional commit message referencing the
  review comment id.
- Emit the required `AWF-VERDICT` line when complete.

## Implementation Steps

1. Add failing regression tests for migration timeout guardrails and
   `add_events()` version/order reservation.
2. Add callback envelope regression coverage for secondary failure events.
3. Update the migration with `lock_timeout` and `statement_timeout` guards.
4. Update `WorkspaceRepository.add_events()` to lock the workspace row, reserve
   event-order values by advancing `workspace.version`, and assign the reserved
   orders to created events.
5. Remove stale manual version bumps in control paths/tests where `add_events()`
   now owns reservation.
6. Update callback API docs to use valid public event types and describe the
   sanitized secondary-failure envelope.
7. Run focused verification and write validation evidence.

## Verification Commands And Pass Criteria

- Before implementation, the new migration/add-events regression tests should
  fail where practical.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_migration_graph.py::test_workspace_event_order_migration_has_timeout_guardrails tests/unit/db/test_workspace_repository.py::TestAddEvents::test_batch_reserves_event_order_and_advances_workspace_version -q`
  must pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_secondary_failure_callback_envelope_excludes_internal_causality_payload -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_preserves_existing_validation_failure tests/unit/service/test_failure_causality.py::test_remonitor_reset_event_order_precedes_same_tick_failure tests/unit/service/test_failure_causality.py::test_failure_causality_snapshot_reads_secondary_failure_recorded_events -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/controls.py tests/unit/db/test_migration_graph.py tests/unit/db/test_workspace_repository.py tests/unit/service/test_callbacks.py`
  must pass.
