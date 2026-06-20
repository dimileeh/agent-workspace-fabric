# Protected Repair Missing HEAD Restore Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6K-COS` reports that protected-scope repair can leave
the worktree branch pointing at a commit whose object exists only in a private
`GIT_OBJECT_DIRECTORY`. When the post-repair HEAD-object verification fails, the
runner raises a reason-coded failure without restoring the branch to the
pre-repair head.

Scope is limited to the protected-scope repair helper and focused regression
coverage for that behavior.

## Requirements Checklist

- Capture the pre-repair HEAD before invoking the repair agent.
- If post-repair HEAD-object verification fails, restore the worktree ref to the
  captured pre-repair HEAD before raising `_MonitorHeadObjectMissingError`.
- Run restore Git commands without object-lookup override environment variables.
- Preserve the existing reason code and failure classification.
- Keep the change minimal and avoid broad validation; AWF/GitHub owns full
  validation after agent completion.

## Implementation Steps

1. Add a focused unit regression that simulates a protected-scope repair agent
   self-commit followed by missing HEAD-object verification.
2. Confirm the regression fails on current code because no reset to the
   pre-repair HEAD occurs.
3. Update `_repair_protected_scope_changes_before_commit` to snapshot HEAD before
   the repair agent and reset hard to that snapshot before raising the missing
   HEAD-object error.
4. Re-run the focused test file or narrowed test selection.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q`
  should pass after implementation.
- If confirming red/green is practical, the new test should fail before the
  implementation because the reset call is absent.
