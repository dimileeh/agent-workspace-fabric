# PRRT_kwDOSJAM6s6K-aI1 Plan

## Problem Statement And Scope

The review thread reports that an agent `ComposeExecCleanupError` followed by
missing-HEAD recovery can call `_verify_recovered_post_agent_commit_or_mark_failed`,
which may pause the workspace as `blocked` for a protected-scope violation. The
caller currently treats the helper's `False` result like ordinary recovery
failure, rethrows the cleanup error, and the outer cleanup handler then attempts
`_mark_failed(from_status=running)`. Because the workspace is already `blocked`,
that failed transition can be stale and leave the original cleanup failure
resumable as a protected approval.

Scope is limited to the agent cleanup failure + missing-HEAD recovery verification
path in `execution_flow.py` and focused regression coverage.

## Requirements Checklist

- Verify the reported path against current code before changing behavior.
- Add a focused regression for agent cleanup failure recovery where recovered
  commit verification blocks for protected scope.
- Preserve existing behavior when missing-HEAD recovery itself fails: terminal
  `EXEC_PROCESS_CLEANUP_FAILED` from `running`.
- Preserve existing successful recovery verification behavior: cleanup failure is
  still terminal `EXEC_PROCESS_CLEANUP_FAILED`.
- When verification has already moved the workspace to `blocked`, convert that
  outcome to the original cleanup infrastructure failure instead of leaving a
  protected approval resume path.
- Keep changes minimal and avoid broad AWF/GitHub validation.

## Implementation Steps

1. Add a targeted regression to `tests/unit/control/test_executor_mirror_hooks_path_commit.py`
   that makes recovered verification return `False` after recording a protected
   block and asserts cleanup failure is marked from `blocked`.
2. Run the new test first and confirm it fails against current behavior.
3. Change the local cleanup recovery helper/caller to distinguish verification
   blocked from ordinary recovery failure and mark the original cleanup failure
   from `blocked` before returning.
4. Run the new regression plus adjacent cleanup recovery tests.
5. Create the validation document with focused command evidence and note broad
   validation remains AWF/GitHub-owned after agent completion.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_fails_blocked_agent_cleanup_recovery_verification_protected_scope -q`
  fails before the implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_preserves_agent_cleanup_failure_when_head_recovery_fails tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_fails_blocked_agent_cleanup_recovery_verification_protected_scope -q`
  passes.
- Full AWF/GitHub validation is intentionally not run inside this agent phase.
