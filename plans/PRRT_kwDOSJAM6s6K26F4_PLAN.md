# PRRT_kwDOSJAM6s6K26F4 Plan

## Problem Statement and Scope

The PR review thread reports that missing-HEAD filesystem recovery can leave
dirty or staged residue when it aborts after rewriting the mirror branch ref and
resetting/staging the worktree. Scope is limited to the recovery abort cleanup
path in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and focused
regression coverage for that behavior.

## Requirements Checklist

- Verify abort paths after the recovery mutation point roll the worktree back to
  `operation_start_head`.
- Preserve the existing supply-chain policy block behavior and warning.
- Keep the fix scoped to missing-HEAD filesystem recovery.
- Add a focused regression test that fails before the cleanup fix.

## Implementation Steps

1. Inspect existing missing-HEAD recovery tests and helpers.
2. Add a regression for a post-staging abort, such as a failed commit command,
   asserting no staged or worktree residue remains.
3. Implement minimal cleanup before late `None` returns in missing-HEAD
   filesystem recovery.
4. Run the focused test for the new/changed behavior only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused test> -q`
  - Passes with the new regression.

Full AWF/GitHub validation remains managed by AWF after agent completion.
