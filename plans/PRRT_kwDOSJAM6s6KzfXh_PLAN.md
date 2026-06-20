# PRRT_kwDOSJAM6s6KzfXh Plan

## Problem Statement and Scope

Missing-HEAD filesystem recovery in `_commit_dirty_worktree` can advance `HEAD`
with a recovered commit, then validate protected scope through dirty-worktree
status. Because the worktree now matches `HEAD`, dirty-status diff loading can
miss protected protected-file changes introduced by the recovered commit. The
fix is limited to recovered commits handled by `_commit_dirty_worktree`.

## Requirements Checklist

- Add a focused regression test for recovered committed protected-scope changes.
- Validate recovered commit ranges as committed diffs against the recovery base.
- Block recovered commits that contain unowned protected-scope changes, including
  when compose repair context is unavailable.
- Preserve existing behavior for runtime-only recovered diffs and normal dirty
  worktree commits.
- Run focused tests only; broad AWF/GitHub validation remains managed after the
  agent phase.

## Implementation Steps

1. Add a unit regression under the existing PR monitor runner `_commit_dirty_worktree`
   tests for `operation_start_head..recovered` changing a protected path.
2. Update `_commit_dirty_worktree` missing-HEAD recovery handling to load
   protected file diffs from the recovered committed range.
3. Raise the existing monitor policy block when recovered committed violations
   remain.
4. Run the targeted unit test module or selected tests covering the changed path.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`
  should pass.
- Full AWF/GitHub validation is intentionally not run in this workspace phase.
