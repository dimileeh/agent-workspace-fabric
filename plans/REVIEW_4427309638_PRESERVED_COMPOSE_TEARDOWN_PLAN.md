# Review 4427309638 Preserved Compose Teardown Plan

## Problem Statement And Scope

Cursor Bugbot's review summary points to two compose-cleanup issues. The
auth-overlay no-candidate fallback is already addressed by the current local
commit. The remaining issue is that single-workspace filesystem GC invokes the
compose teardown callback only for delete candidates or a missing workspace row.
When the row exists but is preserved, monitor completion cleanup records a
preserved plan and skips the volume-removing compose teardown.

Scope is limited to the single-workspace GC no-delete-candidate path used by
monitor completion cleanup. Preserved workspaces must remain preserved: no path
deletion, lease revocation, or reservation release should be widened.

## Requirements Checklist

- Add a regression test proving a preserved row-backed single-workspace GC run
  still invokes the compose teardown callback.
- Preserve the existing GC decision: preserved rows stay out of
  `plan.candidates` and their pressure directories are not deleted.
- Use stored workspace compose metadata for the fallback teardown candidate when
  the row exists.
- Keep the existing missing-row fallback behavior intact.
- Run only focused checks for the touched behavior; AWF owns broad validation
  after agent completion.

## Implementation Steps

1. Add a focused regression test under the service GC single-workspace tests for
   a completed, unmerged PR workspace that is preserved despite
   `ignore_retention=True`.
2. Confirm the regression fails before implementation when practical.
3. Update `run_workspace_filesystem_gc` to build a fallback compose-teardown
   candidate from the preserved workspace row when there are no delete
   candidates.
4. Keep `_run_gc_compose_teardowns` limited to real plan candidates first and
   the fallback candidate only when there are no candidates.
5. Run the new focused test and a nearby missing-row/monitor completion check,
   plus focused lint for changed Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_tears_down_compose_for_preserved_workspace -q`
  - Fails before implementation because the compose callback is not invoked.
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "empty_plan or auth_overlay"`
  - Passes after implementation to preserve the existing missing-row and
    auth-overlay no-candidate fixes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py tests/unit/service/test_gc_parts/test_gc_part_001.py`
  - Passes.

Full AWF/GitHub validation is intentionally not run in the agent phase.
