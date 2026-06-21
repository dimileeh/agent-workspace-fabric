# PRRT_kwDOSJAM6s6KySdC Plan

## Problem Statement and Scope

The fix-cycle per-item operation anchor currently uses `rev-parse HEAD` only.
If a previous item leaves `HEAD` pointing at a commit object missing from the
worktree mirror, `rev-parse` can still return that SHA and the next item receives
an unusable `operation_start_head`. Scope is limited to selecting a valid anchor
for each inline thread/review item in `fix_cycle.py`.

## Requirements Checklist

- Verify a parsed per-item `HEAD` has an existing commit object before using it.
- Fall back to the cycle-opening `operation_start_head` when the current `HEAD`
  ref is missing or points at a missing commit object.
- Add a focused regression test for a poisoned per-item `HEAD`.
- Keep changes scoped to the fix-cycle behavior and its tests.

## Implementation Steps

1. Add a failing unit regression covering two fix-cycle items where the second
   item sees a parsed but unresolvable current `HEAD`.
2. Update `_current_item_operation_start_head` to check `git cat-file -e
   <sha>^{commit}` before returning the current head.
3. Run only targeted tests for the new/changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py -q -k poisoned`
  should pass.
- Full AWF/GitHub validation remains managed by AWF after agent completion per
  workspace contract.
