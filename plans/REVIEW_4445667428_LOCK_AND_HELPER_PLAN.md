# Review 4445667428 Lock And Helper Plan

## Problem Statement And Scope

Address the latest review-level feedback from PR comment `issue:4445667428`
about failure-causality preservation. The review cites three areas: a missing
JSON dialect caveat, an already-failed cleanup event-order race, and a worker
test helper that seeds primary failure evidence in a non-production row state.

## Requirements Checklist

- Verify whether `_failure_epoch_reset_conditions` already documents the
  Postgres-only JSON predicate; change only if the caveat is missing.
- Ensure the already-failed cleanup failure path records
  `workspace.secondary_failure_recorded` with a workspace version/event order
  derived from a row locked for update.
- Update worker failure-causality tests so primary failure evidence is seeded by
  a real failed transition before secondary recovery paths are exercised.
- Preserve existing regression assertions for primary failure reason, message,
  validation provenance, and secondary failure payloads.
- Validate with the narrow affected tests and static checks when practical.

## Implementation Steps

1. Inspect the current JSON predicate comments and leave the existing caveat in
   place if present.
2. Add or adjust regression coverage for already-failed cleanup failure event
   ordering if the current test surface does not cover version ordering.
3. Make the cleanup callback refresh explicitly lock the workspace row before
   status-dependent cleanup result handling.
4. Refactor `_seed_primary_failure_evidence` in `tests/unit/control/test_worker.py`
   so failed events are produced through `WorkspaceRepository.transition`.
5. Run focused tests for the changed control worker scenarios and controls
   cleanup branch, plus lint/type checks if feasible.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<focused tests> -q`
  passes for the worker causality scenarios touched by the helper.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py::test_destroy_cleanup_failure_records_secondary_when_workspace_already_failed -q`
  passes and proves secondary cleanup event ordering.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/control/test_worker.py tests/unit/service/test_controls.py`
  passes.
