# Address Thread PRRT_kwDOSJAM6s6HEtQO Plan

## Problem Statement And Scope

The review reports that `run_workspace_filesystem_gc(..., cleanup_enabled=False, execute=True)`
still performs missing-row fallback compose teardown. The fix is scoped to the
single-workspace filesystem GC missing-row fallback behavior and a focused
regression test.

## Requirements Checklist

- Verify the current missing-row fallback path against `cleanup_enabled`.
- Preserve fallback compose teardown for missing rows when cleanup is enabled.
- Skip missing-row fallback compose teardown when cleanup is disabled.
- Keep the plan payload's `cleanup_enabled` policy state unchanged.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests for the changed behavior.

## Implementation Steps

1. Add a regression test for a missing workspace row with `cleanup_enabled=False`,
   `execute=True`, and a compose teardown hook.
2. Confirm the regression fails against the current implementation.
3. Gate missing-row fallback candidate creation on `cleanup_enabled`.
4. Re-run the focused regression and the adjacent missing-row fallback test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_cleanup_disabled_skips_missing_workspace_fallback_compose_teardown -q`
  - Passes after the fix and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py::test_single_workspace_gc_reports_failed_missing_workspace_compose_teardown -q`
  - Confirms cleanup-enabled missing-row fallback behavior remains intact.

Full AWF/GitHub validation is managed after agent completion.
