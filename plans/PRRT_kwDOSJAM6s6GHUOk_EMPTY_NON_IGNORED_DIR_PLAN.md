# Empty Non-Ignored Directory Guard Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GHUOk` reports that validation-created empty
non-ignored directories are invisible to `git status --porcelain=v1
--untracked-files=all`. Cleanup can then return success while leaving filesystem
state that is not represented in a commit and will be missing in a fresh
checkout.

Scope is limited to validation worktree cleanliness and cleanup behavior in
`src/awf/runtime/validation_worktree.py`, focused unit tests in
`tests/unit/runtime/test_validation_worktree.py`, and this plan/validation
record. Full AWF/GitHub validation remains outside the agent phase.

## Requirements Checklist

- Add a regression test proving an empty non-ignored directory is treated as
  validation worktree dirt even when git status reports no paths.
- Add a regression test proving cleanup removes a validation-created empty
  non-ignored directory before reporting success.
- Preserve existing ignored-directory snapshot and cleanup behavior.
- Run only focused tests and checks for the touched validation worktree code.

## Implementation Steps

1. Add the failing regression tests for empty non-ignored directories.
2. Extend `check_validation_worktree_clean` to snapshot fileless non-ignored
   directory trees from the filesystem, excluding ignored roots reported by git.
3. Include those directory paths in the dirty path and untracked path sets so
   existing cleanup uses `git clean -fdx`.
4. Run the new tests before and after implementation, then run a focused
   validation worktree unit-test slice and narrow lint/type checks.

## Verification Commands and Pass Criteria

- New regression tests fail before implementation when practical:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_empty_untracked_dirs_as_dirty tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_untracked_dir_after_validation -q`
- After implementation, the new regression tests pass.
- Focused validation worktree tests covering touched cleanup behavior pass.
- `ruff` and `mypy` pass for the changed runtime module and focused test file.
- Full AWF/GitHub validation is not run locally; AWF owns broad validation after
  agent completion.
