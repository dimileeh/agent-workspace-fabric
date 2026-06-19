# Pre-Push Fix Mirror Repair Exception Plan

## Problem Statement And Scope

The pre-push validation fix pass repairs the shared mirror hooks path before
launching the fix agent and after successful agent execution. It also repairs
after `ComposeExecCleanupError`, but the generic exception handler rolls back
the worktree and returns without repairing the mirror. A failing agent that
poisons `core.hooksPath` before raising a generic exception can leave the shared
mirror polluted for sibling workspaces.

Scope is limited to the generic exception path in
`src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py` and a focused
regression test.

## Requirements Checklist

- Add a regression test proving generic fix-agent exceptions repair the mirror
  after rollback.
- Preserve existing rollback failure behavior: rollback failure still wins over
  mirror repair success.
- Fail closed if mirror repair after a generic exception fails.
- Do not broaden validation beyond focused tests for the touched behavior.

## Implementation Steps

1. Add a focused unit test near existing pre-push fix-pass mirror repair tests.
2. Confirm the new test fails against the current implementation when practical.
3. Update the generic exception handler to run
   `_repair_pre_push_validation_fix_mirror_hooks` after rollback, matching the
   cleanup-error path.
4. Run the focused regression test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k "generic_exception_repairs_hooks_path or cleanup_error_repairs_hooks_path"`

Pass criteria: the focused regression and adjacent cleanup-error test pass.
Full AWF/GitHub validation is managed by AWF after agent completion.
