# PRRT K8PSM HooksPath Repair Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6K8PSm` reports that `repair_mirror_hooks_path`
only repairs `core.hooksPath` in the shared bare mirror config. Git can also
store worktree-local config in `mirror.git/worktrees/<worktree>/config.worktree`,
which `git -C <worktree> commit` honors when `extensions.worktreeConfig` is
enabled.

Scope is limited to repairing worktree-local `core.hooksPath` values for linked
worktrees under the same bare mirror and proving the regression with focused
unit coverage.

## Requirements Checklist

- Add a regression test showing `repair_mirror_hooks_path` clears
  `core.hooksPath` from linked worktree config.
- Preserve the existing mirror-local repair behavior and return semantics.
- Raise `GitOperationError` with existing repair reason code if worktree-local
  config repair fails.
- Avoid broad validation; AWF/GitHub own full validation after agent completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/node/test_git_manager_mirror_hooks_repair.py`.
2. Extend `repair_mirror_hooks_path` to inspect `mirror.git/worktrees/*/config.worktree`.
3. Reuse the existing unset-pattern behavior for exact `core.hooksPath` removal.
4. Run the focused node unit tests for mirror hook repair.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py tests/unit/node/test_git_manager_mirror_hooks_path_errors.py -q`
  - Passes without failures.
- Do not run full coverage or whole-repository validation in this agent phase.
