# Review 4620252998 GC Teardown Plan

## Problem Statement And Scope

Address PR review comment `issue:4620252998` about completed-workspace GC compose
teardown tracking, empty-plan auth-overlay unmount ordering, and the implicit
no-callback side-effect behavior in workspace GC.

Scope is limited to the reviewed lifecycle/GC code, focused regression coverage,
and this plan/validation record. Do not push or run AWF/GitHub-owned broad
validation.

## Requirements Checklist

- Verify the review claims against the current code before editing.
- Keep compose teardown callback failure logging consistent between the monitor
  tracking wrapper and GC's recorded compose teardown result.
- Ensure empty-plan auth-overlay unmount happens only after partial GC status is
  handled, so partial failures do not silently unmount before the failure log.
- Document that no compose callback means all candidate side effects proceed.
- Add focused regression tests for changed behavior.
- Run narrow validation only for the touched tests/code path.
- Commit the scoped changes locally with a conventional commit message.

## Implementation Steps

1. Add failing focused lifecycle tests for shared callback failure result tracking
   and for skipping empty-plan auth-overlay unmount on partial GC results.
2. Update lifecycle/GC implementation minimally to satisfy those tests.
3. Add an inline GC comment for the intentional no-callback behavior.
4. Run the focused unit tests that cover the changed lifecycle and GC behavior.
5. Create `plans/REVIEW_4620252998_GC_TEARDOWN_VALIDATION.md` with requirement
   status and evidence.
6. Stage only changed files and commit locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_shared_callback_failure_result_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_skips_empty_plan_auth_overlay_unmount_on_partial_result tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_tracks_callback_raised_when_gc_raises_after_teardown tests/unit/runtime/test_monitor_completion_gc.py::test_completed_workspace_gc_unmounts_auth_overlay_when_plan_is_empty tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_records_raised_missing_workspace_compose_teardown -q`

Pass criteria: all listed tests pass. Full AWF/GitHub validation remains managed
by AWF after agent completion.
