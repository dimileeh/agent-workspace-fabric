# Address Review 4620252998 Plan

## Problem Statement And Scope

The review-level PR comment reports two remaining compose-teardown
observability defects in completed workspace filesystem GC:

- `_run_gc_compose_teardowns` lets unexpected `Exception` raises from the
  teardown callback escape instead of converting them into a structured failed
  `WorkspaceGCComposeTeardownResult`. That bypasses the normal partial-result
  path and skips the explicit side-effect gating signal.
- `monitor.filesystem_gc_deferred` is emitted for preserved completed workspaces
  without a self-contained field showing that compose teardown already ran and
  succeeded.

Scope is limited to the GC compose teardown normalization path, the completed
monitor deferred log payload, and focused regressions for those two behaviors.
Existing earlier fixes for partial failure logging are preserved.

## Requirements Checklist

- Verify the reviewer claims against the current service GC and lifecycle code.
- Add a service-level regression proving a raising compose teardown callback is
  reported as a structured failed teardown and does not escape
  `run_workspace_filesystem_gc`.
- Add a lifecycle-level regression proving `monitor.filesystem_gc_deferred`
  includes `compose_teardown_status` when a compose teardown result exists.
- Implement the smallest code changes needed to satisfy those regressions while
  allowing `asyncio.CancelledError` to propagate.
- Run only focused tests and targeted lint for touched files; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add focused unit tests in `tests/unit/service/test_gc_parts/test_gc_part_002.py`
   and `tests/unit/runtime/test_monitor_completion_gc.py`.
2. Confirm the new tests fail against the current implementation when practical.
3. Update `_run_gc_compose_teardowns` to catch ordinary `Exception` from
   `_run_compose_teardown` and record a failed
   `WorkspaceGCComposeTeardownResult`.
4. Update `_gc_completed_workspace_filesystem` deferred logging to include
   `compose_teardown_status` when the result carries one for the workspace.
5. Re-run focused tests and targeted lint, then record validation evidence.

## Verification Commands And Pass Criteria

- Initial focused regression command:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_records_raised_missing_workspace_compose_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_preserved_success_deferred_log_includes_compose_teardown_status -q`
  should fail before implementation.
- Final focused regression command should pass after implementation.
- Optional focused file slices:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_records_raised_missing_workspace_compose_teardown tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_reports_failed_missing_workspace_compose_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_preserved_success_deferred_log_includes_compose_teardown_status tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_preserved_success_still_logs_filesystem_gc_deferred -q`
- Optional targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/service/test_gc_parts/test_gc_part_002.py tests/unit/runtime/test_monitor_completion_gc.py`
  should pass.
