# PRRT_kwDOSJAM6s6B9EBk Plan

## Problem Statement And Scope

The review thread reports that `_fail_stale_active_execution` preserves a
primary failure in the transition payload but does not repopulate
`Workspace.failure_reason` and `Workspace.failure_message` when those row fields
were cleared during remonitor recovery.

Scope is limited to the worker paths that fail active work while preserving a
primary failure snapshot.

## Requirements Checklist

- Add a regression for stale active execution failure with a preserved primary
  snapshot and cleared row-level failure fields.
- Populate row-level failure fields from the primary snapshot before commit when
  preserving primary evidence.
- Keep fallback infrastructure failure behavior unchanged when no primary
  snapshot exists.
- Preserve the existing transition payload with primary and secondary failure
  evidence.

## Implementation Steps

1. Add a focused failing unit test for `_fail_stale_active_execution`.
2. Update the worker preservation branch to restore row-level primary evidence.
3. Apply the same worker-local preservation helper to runtime stranding, which
   has the same preserved-primary branch shape.
4. Run targeted tests and lint for the touched files.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6B9EBk_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_fail_stale_active_execution_restores_primary_failure_row_fields -q`
  fails before the implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_fail_stale_active_execution_restores_primary_failure_row_fields tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_stale_active_execution_preserves_validation_failure_and_records_secondary_stale tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_runtime_stranding_preserves_provider_auth_primary_failure -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker_coverage_edges.py`
  passes.
