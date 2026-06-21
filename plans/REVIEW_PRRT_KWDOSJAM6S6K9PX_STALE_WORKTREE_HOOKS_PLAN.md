# Stale Worktree Hooks Repair Review Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K9pX-` reports that
`repair_mirror_hooks_path` iterates `$GIT_DIR/worktrees/*`, resolves stale
linked-worktree metadata for a deleted sibling worktree, and then runs
`git -C <missing>` during hooks-path repair. That can fail unrelated
workspaces with `MIRROR_HOOKS_PATH_REPAIR_FAILED` before Git has pruned stale
worktree entries.

Scope is limited to skipping stale linked-worktree entries whose worktree
directory no longer exists before probing their local or worktree config.

## Requirements Checklist

- Verify the review claim against local code and existing tests.
- Add focused regression coverage for stale linked-worktree metadata left under
  a mirror's `worktrees` directory.
- Preserve repair behavior for existing linked worktrees and mirror config.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests for touched
  behavior.

## Implementation Steps

1. Add a unit test that creates a mirror with a linked worktree, deletes the
   linked worktree directory without pruning metadata, and verifies mirror hook
   repair still succeeds.
2. Update `repair_mirror_hooks_path` to skip a linked worktree entry when the
   resolved worktree path no longer exists.
3. Run the new regression test, then the focused mirror hook repair unit file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_skips_stale_linked_worktree_entry -q`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  - Passes after implementation.

Full AWF/GitHub validation remains managed by AWF after agent completion.
