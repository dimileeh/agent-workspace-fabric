# PRRT_kwDOSJAM6s6D5kjQ Symlinked Git Backref Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6D5kjQ` reports that runtime ownership repair
can accept a suffixed linked-worktree gitdir back-reference when the workspace
`.git` path is a symlink. `_validate_linked_git_dir_backref` resolves both the
metadata `gitdir` back-reference and the expected `worktree/.git` path, so a
symlinked workspace `.git` can make another workspace's metadata appear to match.

Scope is limited to rejecting symlinked workspace `.git` files during suffixed
linked-worktree back-reference validation and adding a regression test.

## Requirements Checklist

- Add a regression test that demonstrates a symlinked workspace `.git` backref
  is rejected and ownership repair is not called.
- Preserve valid numeric-suffix linked worktree behavior when `worktree/.git` is
  a normal Git control file.
- Implement the smallest ownership validator change needed to reject the unsafe
  symlink shape before resolving the expected `.git` path.
- Validate with the focused unit test file.

## Implementation Steps

1. Add the failing regression to `tests/unit/runtime/test_ownership.py`.
2. Update `_validate_linked_git_dir_backref` in `src/awf/runtime/ownership.py`
   to require the expected workspace `.git` path to be a regular non-symlink
   file before resolving and accepting the back-reference.
3. Run the focused regression to confirm failure, then implementation, then
   green validation.
4. Run narrow style/type checks when practical for the touched area.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/ownership.py` should
  pass if mypy can run on the single module in this repo configuration.
