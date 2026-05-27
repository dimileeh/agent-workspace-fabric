# PRRT_kwDOSJAM6s6FPNWA Companion Worktree Skip Plan

## Problem Statement And Scope

The review thread reports that `WorkspaceCleaner.cleanup(remove_worktree=False)` records only the
primary `worktree_remove` skip step, even when companion worktrees are supplied. The remove branch
records one step per primary and companion worktree, so the skip branch should expose the same
target coverage for monitoring and debugging.

Scope is limited to `src/awf/node/cleanup.py` and focused node cleanup regression coverage.

## Requirements Checklist

- Record a skipped cleanup step for the primary worktree when `remove_worktree=False`.
- Record a skipped cleanup step for each companion worktree when `remove_worktree=False`.
- Preserve existing behavior that no git worktree removal is attempted when worktree cleanup is
  disabled.
- Keep successful cleanup status because skipped steps are non-failing outcomes.
- Do not run broad AWF/GitHub-owned validation; run only focused checks for the touched behavior.

## Implementation Steps

1. Add failing regression coverage in `tests/unit/node/test_cleanup.py` for companion worktrees with
   `remove_worktree=False`.
2. Update `WorkspaceCleaner.cleanup` to append skipped outcomes for the same primary and companion
   target names used by the remove branch.
3. Run the focused regression test, then the focused node cleanup test file.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_cleanup.py -q -k companion_worktree_skip`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_cleanup.py -q`

Pass criteria: the focused regression fails before implementation, then both focused commands pass
after implementation. Full AWF/GitHub validation remains managed by AWF after agent completion.
