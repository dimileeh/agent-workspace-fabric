# PRRT_kwDOSJAM6s6FL6d9 Plan

## Problem Statement and Scope

The default terminal-GC worktree remover returns `WORKTREE_NOT_GIT_MANAGED` when the primary worktree directory exists without a `.git` marker. That early return prevents valid companion worktrees from being removed through `GitManager.remove_worktree`.

Scope is limited to `src/awf/service/gc.py` default worktree removal behavior and a focused unit regression in `tests/unit/service/test_gc_more2.py`.

## Requirements Checklist

- Add a regression test proving a plain primary worktree does not block companion git worktree removal.
- Preserve the existing skip behavior when there are no git-managed targets to remove.
- Keep removal attempts best-effort across remaining targets and preserve existing failure reporting.
- Avoid broad AWF/GitHub-owned validation; record focused checks only.

## Implementation Steps

1. Add a failing unit test for a candidate with a plain primary worktree and a git-managed companion.
2. Update `_default_worktree_remover` to skip only non-git-managed existing target directories while continuing with other targets.
3. Run the focused test before and after the implementation.
4. Create a validation document with requirement status and command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_more2.py -q -k "plain_directory"`

Pass criteria: the focused regression fails before the implementation and passes after the implementation. Full AWF/GitHub validation remains managed by AWF after agent completion.
