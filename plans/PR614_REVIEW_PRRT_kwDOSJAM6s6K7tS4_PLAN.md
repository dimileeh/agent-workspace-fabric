# PR614 Review Thread PRRT_kwDOSJAM6s6K7tS4 Plan

## Problem Statement And Scope

The reviewer reports that the allowlisted mirror `core.hooksPath` value
`.githooks/Lefthook` is preserved when a registered worktree contains only an
empty hooks directory. Git treats `core.hooksPath` as the directory to search for
hook files, so an empty allowlisted directory still bypasses installed hooks.

Scope is limited to mirror hook-path repair in `src/awf/node/git_manager.py`,
focused regression coverage in `tests/unit/node/test_git_manager_mirror_hooks_repair.py`,
and this review-thread plan/validation pair.

## Requirements Checklist

- Reproduce the empty allowlisted directory bypass with a focused regression
  test before implementation.
- Preserve `.githooks/Lefthook` only when every registered worktree has that
  hooks directory and it contains the expected executable `pre-commit` hook.
- Keep existing poisoned and unrecognized hook-path cleanup behavior unchanged.
- Run only targeted tests/checks for the touched files; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Update the legitimate hooks-path fixtures to create an executable Git hook
   file under `.githooks/Lefthook`.
2. Add regressions for an empty `.githooks/Lefthook` directory and a
   non-executable hook file.
3. Tighten `_mirror_has_registered_hooks_path()` so it fails closed unless the
   allowlisted hooks directory contains the expected executable `pre-commit`
   hook file.
4. Run the focused unit tests for mirror hook-path repair and a focused ruff
   check on touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  - Passes all focused mirror hook-path repair tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - Reports no lint failures in touched files.
