# PRRT_kwDOSJAM6s6KLm5G Plan

## Problem Statement and Scope

The fix cycle captures one `operation_start_head` before its multi-pass comment
repair loop and passes that same SHA into every thread and review-comment
repair. Later repairs should use the current worktree HEAD as their per-agent
recovery anchor so missing-HEAD recovery cannot re-anchor onto the cycle-opening
tip and lose intervening local fix commits.

Scope is limited to the PR monitor fix-cycle item invocation path and focused
regression coverage for thread/comment anchor threading.

## Requirements Checklist

- Keep the cycle-opening `operation_start_head` for final push validation and
  protected-scope whole-cycle provenance.
- Capture the current worktree HEAD immediately before each thread/comment agent
  repair and pass that per-item SHA to the repair helper.
- Fall back to the cycle-opening SHA if a per-item HEAD cannot be read, matching
  the existing fail-closed recovery behavior without broad refactors.
- Add focused regression coverage proving later fix-cycle items receive the
  updated post-commit HEAD.
- Do not run broad AWF/GitHub-owned validation; only run targeted tests/checks
  for the changed behavior.

## Implementation Steps

1. Add a small local helper in `_run_fix_cycle` to read the current worktree HEAD
   for the next item.
2. Use that helper for `_address_thread` and `_address_review_comment_result`
   calls while leaving push/protected-scope calls on the original cycle anchor.
3. Add a unit regression that simulates a prior item moving HEAD and asserts the
   later item receives that updated anchor.
4. Run the focused pytest target for the new regression and record results.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k "fix_cycle_uses_current_head_as_per_item_recovery_anchor"`
  - Passes with the new regression.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation after completion.
