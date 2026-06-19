# PRRT_kwDOSJAM6s6Kw8RS Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6Kw8RS` reports that `_remove_empty_untracked_dirs`
can delete earlier empty untracked directories before a later `git check-ignore`
probe raises `_IgnoreCheckError`. A failed validation-worktree cleanliness check
should not partially mutate the worktree.

## Scope

- Touch only validation worktree empty-directory cleanup behavior and focused
  regression coverage.
- Preserve existing successful cleanup behavior, including ignored-root and
  git-boundary handling.
- Do not run broad AWF/GitHub-owned validation.

## Requirements Checklist

- [ ] Add a regression proving `check-ignore` failure during cleanup leaves
  previously discovered empty directories in place.
- [ ] Make `_remove_empty_untracked_dirs` complete ignore probing before any
  `rmdir` mutation.
- [ ] Keep returned cleanup paths limited to directories actually removed.
- [ ] Keep the code change minimal and localized.

## Implementation Steps

1. Add a unit test that creates two empty directories, makes the later
   `check-ignore` probe fail, and asserts both directories still exist.
2. Refactor `_remove_empty_untracked_dirs` into a collect-then-remove flow:
   collect removable empty directory candidates after all ignore checks pass,
   then remove candidates depth-first.
3. Run focused tests for validation worktree empty-directory cleanup.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::<new-test> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -k remove_empty_untracked_dirs -q`

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
