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

## Iteration 2: Compose-Only Success Observability

### Problem Statement And Scope

Follow-up review on the same PR comment identified two remaining issues:

- `_gc_result` has a `compose_teardown_failed` flag that appears redundant for
  normal candidates but is required for fallback compose teardown candidates,
  where the path deletion loop never records a delete error.
- A missing-workspace fallback compose teardown can succeed and emit
  `monitor.filesystem_gc_ok` with `deleted_path_count=0`, making the event look
  like a clean no-op unless the operator correlates it with the separate
  `monitor.compose_teardown_ok` event.

Scope is limited to one explanatory comment in service GC, one success-log
payload change in the completed monitor filesystem GC path, and focused runtime
coverage for the compose-only success log.

### Requirements Checklist

- Verify the follow-up reviewer claims against the current service GC and
  lifecycle code.
- Add a focused lifecycle regression proving a missing-workspace fallback
  compose teardown success marks `monitor.filesystem_gc_ok` as compose-only.
- Add a short code comment explaining why `compose_teardown_failed` remains
  load-bearing for fallback candidates.
- Implement the smallest log payload change needed to make compose-only success
  distinguishable from a zero-delete no-op.
- Run only focused tests and targeted lint for touched files; broad AWF/GitHub
  validation remains managed after agent completion.

### Implementation Steps

1. Add the focused runtime regression to
   `tests/unit/runtime/test_monitor_completion_gc.py`.
2. Confirm the new assertion fails against the current implementation when
   practical.
3. Update `_gc_completed_workspace_filesystem` to add compose teardown status
   and a compose-only marker to successful filesystem GC logs when the result is
   the missing-workspace fallback shape.
4. Add the explanatory comment near `_gc_result` error-status derivation.
5. Re-run the focused regression and targeted lint, then update validation
   evidence.

### Verification Commands And Pass Criteria

- Initial/final focused regression command:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_ok_marks_empty_plan_compose_only_success -q`
  should fail before implementation and pass after implementation.
- Optional focused related slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_ok_marks_empty_plan_compose_only_success tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tears_down_compose_when_plan_is_empty tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_filesystem_gc_logs_success_for_retained_old_workspace -q`
  should pass.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/runtime/test_monitor_completion_gc.py`
  should pass.

## Iteration 3: Public Compose Teardown Exception Helper

### Problem Statement And Scope

Follow-up review on the same PR comment identified that
`src/awf/runtime/pr_monitor_runner/lifecycle.py` imports the private
`_compose_teardown_result_for_exception` helper from `awf.service.gc`. The
shared helper is intentionally used by both lifecycle's tracking wrapper and
GC's teardown loop so a re-raised callback exception maps to the same structured
`WorkspaceGCComposeTeardownResult`.

Scope is limited to making that shared helper a public GC module API, updating
the cross-module import to the public name, and keeping the existing fallback
compose teardown error-status explanation intact.

### Requirements Checklist

- Verify the private-import reviewer claim against current code.
- Preserve the existing exception-result caching behavior so lifecycle tracking
  and GC result recording remain consistent for the same exception object.
- Stop importing a private GC implementation detail into lifecycle.
- Keep the existing explanatory comment for fallback compose teardown failures
  that can produce `partial` status with an empty `delete_errors` list.
- Run only focused local checks; broad AWF/GitHub validation remains managed
  after agent completion.

### Implementation Steps

1. Promote `_compose_teardown_result_for_exception` to
   `compose_teardown_result_for_exception` with a short docstring explaining
   the shared lifecycle/GC contract.
2. Update GC's internal exception path and lifecycle's tracking wrapper to call
   the public helper.
3. Remove the private helper name from code after verifying no local callers
   remain.
4. Run the focused regressions that exercise compose teardown exception
   normalization and completed-monitor GC fallback logging.

### Verification Commands And Pass Criteria

- Focused regression slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_records_raised_missing_workspace_compose_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_shared_callback_failure_result_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_monitor_missing_workspace_compose_teardown_failure_logs_gc_failed_cause -q`
  should pass.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py src/awf/runtime/pr_monitor_runner/lifecycle.py`
  should pass.
