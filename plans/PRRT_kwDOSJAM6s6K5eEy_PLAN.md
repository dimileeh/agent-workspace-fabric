# PRRT_kwDOSJAM6s6K5eEy Plan

## Problem Statement and Scope

The pre-push validation fix-pass currently repairs shared mirror `core.hooksPath`
before the agent run and on the normal post-agent path. The inline review reports
that `ComposeExecCleanupError` exits before the post-agent repair, so a timed-out
fix agent can leave the shared mirror poisoned for later workspaces.

Scope is limited to the pre-push validation fix-pass cleanup-error path and a
focused regression test for that path.

## Requirements Checklist

- Reproduce the cleanup-error path with a focused unit test.
- Ensure mirror hook repair runs after an agent cleanup failure when a mirror is
  associated with the worktree.
- Preserve the existing failed-fix-pass outcome and rollback behavior.
- Keep broad AWF/GitHub validation delegated to AWF after agent completion.

## Implementation Steps

1. Add a regression test that raises `ComposeExecCleanupError` from the fix agent
   and asserts mirror hook repair is attempted before the function returns.
2. Run the targeted test and confirm it fails before implementation.
3. Update `pre_push_validation_fix_pass.py` so the cleanup-error path attempts
   the mirror hook repair before returning.
4. Re-run the focused test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k cleanup_error_repairs_hooks_path`

Pass criteria: the targeted regression passes after the code change. Full AWF
and GitHub validation remains managed by AWF after agent completion.
