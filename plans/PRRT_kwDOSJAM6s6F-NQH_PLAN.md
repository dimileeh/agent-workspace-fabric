# PRRT_kwDOSJAM6s6F-NQH — Review thread fix

## Problem statement and scope
A monitor push failure with reason `PRE_PUSH_VALIDATION_ROLLBACK_FAILED` is currently classified as a pre-push validation failure but not treated as terminal. This allows recovery loops to continue even though rollback left the worktree dirty, which can cause repeated failing repair cycles.

## Requirements checklist
1. Treat `PRE_PUSH_VALIDATION_ROLLBACK_FAILED` as terminal in `_GitPushResult.terminal_monitor_failure`.
2. Add or extend a regression test to assert rollback-failure terminality.
3. Keep changes scoped to monitor runtime behavior for this thread only.

## Implementation steps
1. Edit `src/awf/runtime/pr_monitor_runner/remote_ops.py` to include `_PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON` in the terminal failure reason-code set.
2. Update `tests/unit/runtime/test_pr_monitor_remote_ops.py` with a focused unit test for `terminal_monitor_failure` on rollback-failure reason.
3. Run focused tests for the updated module and report outcomes.

## Verification commands and pass criteria
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q`
  - New/updated tests pass.
