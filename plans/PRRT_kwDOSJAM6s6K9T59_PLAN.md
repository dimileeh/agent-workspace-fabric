# PRRT_kwDOSJAM6s6K9T59 Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6K9T59` reports that linked worktree
`config.worktree` scanning uses `git config --file`, which does not evaluate
`includeIf "gitdir:..."` entries the same way Git does when commands run in the
worktree. This can let a worktree-local conditional include expose
`core.hooksPath` to later AWF git commands while the repair probe reports clean.

Scope is limited to mirror/worktree `core.hooksPath` repair in
`src/awf/node/git_manager.py` and focused unit regression coverage.

## Requirements

- Add a focused regression showing a `config.worktree` conditional
  `includeIf "gitdir:..."` can expose a poisoned `core.hooksPath`.
- Probe and repair linked worktree config using repository/worktree context so
  Git evaluates gitdir-conditional includes.
- Preserve existing mirror config repair behavior.
- Preserve existing direct worktree-local `core.hooksPath` and plain include
  repair behavior.
- Run only focused validation for the touched behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a failing unit test in
   `tests/unit/node/test_git_manager_mirror_hooks_repair.py` that writes a
   worktree-local `includeIf "gitdir:..."` pointing at a config with
   `core.hooksPath = /dev/null`, then verifies repair removes the active include
   and leaves no active worktree hooks path.
2. Change `repair_mirror_hooks_path()` so each linked worktree config is repaired
   through the actual worktree path, using `git -C <worktree> config --worktree`
   instead of `git config --file <config.worktree>`.
3. Keep direct file path knowledge for origin comparisons and relative include
   resolution.
4. Run the new regression first where practical, then the focused hook-repair
   test module after implementation.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py::TestRepairMirrorHooksPath::test_removes_worktree_gitdir_include_exposing_poisoned_hooks_path -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`

Pass criteria: the new regression and focused hook-repair module pass.
