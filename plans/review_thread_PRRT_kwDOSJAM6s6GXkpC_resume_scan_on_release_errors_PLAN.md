# Review Thread PRRT_kwDOSJAM6s6GXkpC Resume Scan On Release Errors Plan

## Problem Statement and Scope

The terminal-runtime release scan calls the planning-scope auto-retry
safety-net resume scan only after candidate release errors are raised. If one
candidate fails, `_release_terminal_runtime_resources` propagates that failure
before the safety-net scan runs, so previously failed resume attempts are not
retried until a later fully clean release scan.

Scope is limited to:

- `src/awf/control/worker/cleanup.py`
- Focused worker regression coverage in
  `tests/unit/control/test_worker_parts/test_worker_part_042.py`

## Requirements Checklist

- Run `_resume_pending_planning_scope_auto_retries_after_terminal_release`
  during a release scan even when one or more runtime-release candidates fail.
- Preserve existing per-candidate cleanup behavior and continue processing the
  release batch after a candidate error.
- Preserve existing error propagation from `_release_terminal_runtime_resources`
  after the safety-net resume scan has had a chance to run.
- Preserve cancellation semantics: `asyncio.CancelledError` must still stop the
  scan immediately.
- Add focused regression coverage before implementation.

## Implementation Steps

1. Add a regression where one terminal runtime candidate fails during release
   while an already released workspace has a pending planning-scope auto-retry
   resume failure marker.
2. Confirm the new regression fails before implementation because the resume
   safety-net scan is skipped when the release error is raised.
3. Restructure `_release_terminal_runtime_resources` so release errors are
   accumulated, the safety-net resume scan runs, and the accumulated release
   error or `ExceptionGroup` is raised afterward.
4. Run focused tests for the changed worker behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_runs_planning_scope_resume_safety_net_before_raising_release_error -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_continues_batch_when_per_candidate_recording_raises tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_runs_planning_scope_resume_safety_net_before_raising_release_error -q`

Pass criteria:

- The new regression fails before the implementation change.
- The focused worker tests pass after the implementation change.
- Full AWF/GitHub validation is intentionally not run in the agent phase per
  the workspace contract.
