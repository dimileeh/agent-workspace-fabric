# Address PRRT_kwDOSJAM6s6HDSbj Plan

## Problem Statement And Scope

An inline review on PR #396 reports that `run_workspace_filesystem_gc()` can
execute a fallback compose teardown for a missing workspace row, record a failed
teardown in `compose_teardowns`, and still return a successful GC result because
the empty plan has no candidates and no `delete_errors`.

Scope is limited to surfacing failed compose teardown results in the GC execution
status and adding a focused regression for the missing-workspace fallback path.

## Requirements Checklist

- Verify the review against the current `src/awf/service/gc.py` behavior.
- Add a regression test for missing workspace row plus failed fallback compose
  teardown.
- Update GC result calculation so failed compose teardown outcomes make execution
  `partial`.
- Keep existing successful and skipped teardown behavior unchanged.
- Run only focused tests for the changed GC behavior; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add a focused unit test near existing single-workspace GC tests.
2. Confirm the new test fails against the current code when practical.
3. Make the minimal `src/awf/service/gc.py` change to include failed compose
   teardown results in execution error detection.
4. Re-run the focused GC test selection.
5. Record validation evidence in a matching validation artifact.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_002.py -q`
  should pass after the fix.
- If the initial regression command is run before implementation, it should fail
  because the missing-workspace failed teardown is reported as succeeded.
