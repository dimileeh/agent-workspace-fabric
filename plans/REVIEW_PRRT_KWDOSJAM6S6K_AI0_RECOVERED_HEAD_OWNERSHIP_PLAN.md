# REVIEW_PRRT_kwDOSJAM6s6K-aI0 Recovered HEAD Ownership Plan

## Problem Statement And Scope

The recovered missing-HEAD branch in `src/awf/runtime/pr_monitor_runner/remote_repair.py`
can advance the worktree to a recovered commit before checking agent-runtime
ownership. If that ownership repair fails, the branch currently raises without
restoring `recovery_head`.

Scope is limited to the ownership-failure path reported in review thread
`PRRT_kwDOSJAM6s6K-aI0`.

## Requirements Checklist

- Add a focused regression test for recovered missing-HEAD ownership failure in
  `_commit_dirty_worktree`.
- Ensure the recovered delta is cleaned back to `recovery_head` before raising
  `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED`.
- Preserve the existing protected-scope cleanup behavior.
- Run only focused validation for the changed behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a unit test in the existing focused PR monitor runner test file.
2. Confirm the test fails before the implementation change when practical.
3. Call `_cleanup_recovered_missing_head_delta` on the recovered ownership repair
   failure path before raising.
4. Re-run the focused unit test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k recovered_head_ownership`
  must pass after the fix.
- Full AWF/GitHub validation is intentionally not run in this agent phase.
