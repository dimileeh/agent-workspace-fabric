# PRRT_kwDOSJAM6s6K8A5T Plan

## Problem Statement And Scope

The review thread reports that mirror `core.hooksPath` repair still preserves
`.githooks/Lefthook` when the hook directory is backed by files inside an
agent-writable checkout. Executable hook evidence in a mutable worktree is not a
trusted source, so preserving that mirror config can keep AWF commits pointed at
agent-controlled hooks instead of the installed `.git/hooks` directory.

Scope is limited to mirror `core.hooksPath` classification in
`src/awf/node/git_manager.py`, focused regression coverage in
`tests/unit/node/test_git_manager_mirror_hooks_repair.py`, and this
plan/validation pair.

## Requirements Checklist

- Add/update focused regression coverage showing `.githooks/Lefthook` is cleared
  even when an attached worktree contains an executable `pre-commit` there.
- Remove trust in agent-writable worktree hook paths; unsetting mirror
  `core.hooksPath` should restore Git's default trusted `.git/hooks` lookup.
- Keep existing poisoned, unrecognized, duplicate, and concurrent cleanup
  behavior unchanged.
- Run only targeted checks for touched files; AWF/GitHub will run broad
  validation after agent completion.

## Implementation Steps

1. Update the focused mirror hook repair tests to expect checkout-relative
   `.githooks/Lefthook` values to be removed even with executable hook files.
2. Simplify mirror hook-path classification so only explicit poisoned values get
   named poison patterns and every other configured hook path is removed exactly.
3. Remove obsolete mutable-worktree hook trust helpers and tests.
4. Run the focused mirror hook repair unit test file and a focused ruff check on
   touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_mirror_hooks_repair.py -q`
  - Passes all focused mirror hook-path repair tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_mirror_hooks_repair.py`
  - Reports no lint failures in touched files.
