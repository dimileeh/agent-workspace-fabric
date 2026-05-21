# Ownership Symlink Repair Plan

## Problem Statement And Scope

Runtime ownership repair validates the linked worktree mirror path before
calling `repair_agent_writable_worktree`, but the shared chown target runner
still uses non-recursive `os.chown` for mirror-admin entries. `os.chown`
follows symlinks, so a mirror child such as `worktrees` can redirect a
root-owned repair outside the workspace boundary.

Scope is limited to blocking symlink target ownership changes in the existing
ownership repair helper and adding a focused regression test.

## Requirements Checklist

- Add a regression test proving non-recursive chown targets use `os.lchown`
  for symlink entries.
- Preserve existing recursive chown behavior and object-directory exceptions.
- Keep the fix in the shared helper used by executor and monitor repair paths.
- Run the narrow affected test surface.

## Implementation Steps

1. Add a failing unit test in `tests/unit/node/test_git_manager.py` for a
   non-recursive symlink chown target.
2. Update `_chown_targets` in `src/awf/node/git_manager.py` to avoid following
   symlinks for non-recursive targets.
3. Run the targeted test, then the focused node/runtime ownership test files.
4. Record validation results in `plans/OWNERSHIP_SYMLINK_REPAIR_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::test_chown_targets_uses_lchown_for_non_recursive_symlink -q`
  - Passes after the implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py tests/unit/runtime/test_ownership.py -q`
  - Passes with no regressions in the ownership repair surface.
