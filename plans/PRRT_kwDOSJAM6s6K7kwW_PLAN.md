# PRRT_kwDOSJAM6s6K7kwW Plan

## Problem Statement And Scope

The generic unexpected fix-agent exception branch in
`src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py` returns a
rollback failure before repairing the shared mirror hooks path. If the fix agent
poisoned `core.hooksPath` and rollback also fails, future workspaces can inherit
the poisoned mirror.

Scope is limited to the review thread `PRRT_kwDOSJAM6s6K7kwW`.

## Requirements Checklist

- Confirm mirror hooks repair runs in the unexpected fix-agent exception path
  even when rollback fails.
- Preserve existing failure precedence: mirror repair failure should be returned
  before rollback failure; otherwise return the rollback failure.
- Add a focused regression test for the rollback-failure exception path.
- Do not run broad AWF/GitHub-owned validation; use a targeted unit test only.

## Implementation Steps

1. Add a unit regression that simulates an unexpected fix-agent exception plus a
   rollback failure and asserts mirror repair still runs before returning the
   rollback failure.
2. Update the generic exception branch to call
   `_repair_pre_push_validation_fix_mirror_hooks` before honoring the rollback
   failure, matching the adjacent cleanup-failure path.
3. Run the targeted unit test file or specific test case.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_002.py::<new_test> -q`

Pass criteria: the new regression passes and demonstrates mirror repair is
attempted before the rollback failure is returned.
