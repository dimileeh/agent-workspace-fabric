# Address Review 4620252998 Plan

## Problem Statement And Scope

The review-level PR comment reports two observability defects in completed
workspace filesystem GC logging:

- A preserved empty GC plan with failed fallback compose teardown logs
  `monitor.filesystem_gc_deferred` before the partial-status branch, masking the
  failure from `monitor.filesystem_gc_failed` consumers.
- A missing-workspace fallback compose teardown failure logs
  `monitor.filesystem_gc_failed` with empty `delete_errors` and no compose
  teardown details, forcing operators to correlate a separate event.

Scope is limited to `src/awf/runtime/pr_monitor_runner/lifecycle.py` logging and
focused runtime regression tests. The GC execution semantics themselves are
already covered by prior service-level changes.

## Requirements Checklist

- Verify the reviewer claims against the current lifecycle branch order and log
  payload.
- Add a regression for preserved empty-plan compose teardown failure emitting
  `monitor.filesystem_gc_failed`, not `monitor.filesystem_gc_deferred`.
- Add a regression for missing-workspace fallback compose teardown failure where
  `monitor.filesystem_gc_failed` includes the failed compose teardown details
  despite empty `delete_errors`.
- Keep successful preserved workspace behavior logging
  `monitor.filesystem_gc_deferred`.
- Run only focused tests for the touched runtime behavior; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add focused unit tests in `tests/unit/runtime/test_monitor_completion_gc.py`
   using mocked `run_workspace_filesystem_gc` results.
2. Confirm the new tests fail against the current lifecycle implementation when
   practical.
3. Update `_gc_completed_workspace_filesystem` so partial status is logged before
   preserved/deferred status.
4. Add failed compose teardown details to the `monitor.filesystem_gc_failed`
   payload.
5. Re-run the focused runtime tests and record validation evidence.

## Verification Commands And Pass Criteria

- Initial focused regression command:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_preserved_compose_teardown_failure_logs_filesystem_gc_failed tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_missing_workspace_compose_teardown_failure_logs_gc_failed_cause -q`
  should fail before implementation.
- Final focused regression command should pass after implementation.
- Optional targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_monitor_completion_gc.py`
  should pass.
