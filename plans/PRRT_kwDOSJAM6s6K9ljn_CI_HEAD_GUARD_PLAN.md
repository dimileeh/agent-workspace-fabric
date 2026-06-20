# PRRT_kwDOSJAM6s6K9ljn CI HEAD Guard Plan

## Problem Statement and Scope

The CI-repair agent path repairs mirror hooks after a non-`AgentRunError`
adapter/runtime failure, then immediately re-raises the original exception. That
skips `_commit_dirty_worktree`, whose HEAD-object guard verifies or recovers a
branch ref that an agent may have self-committed through private Git object
lookup. Scope is limited to the CI-repair generic exception path in
`ci_ops.py` and focused unit coverage.

## Requirements Checklist

- Preserve fail-closed behavior when post-agent mirror hook repair fails.
- After successful post-agent mirror hook repair for a non-`AgentRunError`, run
  `_commit_dirty_worktree` with the original `operation_start_head`.
- Preserve existing commit-sink reason-code handling for HEAD-object,
  mirror-hooks, ownership, policy, and protected-scope failures.
- Preserve the original adapter/runtime exception when the sink succeeds.
- Keep validation focused; full AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Update the focused CI cleanup-failure regression to require the post-failure
   dirty-worktree sink call and verify `operation_start_head` forwarding.
2. Confirm the targeted regression fails before the implementation.
3. Update `_run_ci_fix` so the generic adapter/runtime exception path stores the
   original exception, repairs hooks, runs the existing sink and sink exception
   handlers, then re-raises the original exception after the guard succeeds.
4. Run the focused regression and narrow lint for the changed files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py::test_ci_fix_cleanup_error_repairs_hooks_path -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
