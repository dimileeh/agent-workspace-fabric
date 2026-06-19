# Validation: Stop recursing into nested Git worktrees

## Plan reference

`plans/NESTED_GIT_WORKTREE_BOUNDARY_PLAN.md`

## Requirement-by-requirement status

- [x] `_remove_empty_untracked_dirs` must not recurse into a directory that contains a `.git` marker; it must treat such a directory as a non-empty descendant and leave it alone.
  - **Status:** Complete
  - **Evidence:** `src/awf/runtime/validation_worktree.py` now checks `(child / ".git").exists()` before the recursive call in `maybe_remove_empty`. Regression test `test_remove_empty_untracked_dirs_treats_nested_git_marker_as_boundary` passes.

- [x] `_snapshot_empty_untracked_dirs` must not recurse into a directory that contains a `.git` marker; it must treat such a directory as a file-descendant boundary (i.e. a non-empty signal) so its empty descendants are not snapshot.
  - **Status:** Complete
  - **Evidence:** `src/awf/runtime/validation_worktree.py` now checks `(child / ".git").exists()` before the recursive call in `has_file_descendant`. Regression test `test_snapshot_empty_untracked_dirs_treats_nested_git_marker_as_boundary` passes.

- [x] The `.git` marker check must happen **before** the recursive call, not after.
  - **Status:** Complete
  - **Evidence:** In both helpers, `(child / ".git").exists()` is evaluated immediately after confirming the child is a non-ignored directory and before the recursive invocation.

- [x] Existing behavior for regular empty directories, ignored roots, symlinks, non-empty directories, and filesystem edges must remain unchanged.
  - **Status:** Complete
  - **Evidence:** All 31 pre-existing tests in `tests/unit/runtime/test_validation_worktree.py` pass after the change.

- [x] Regression tests must cover both helpers with a nested `.git` directory boundary.
  - **Status:** Complete
  - **Evidence:** Added `test_remove_empty_untracked_dirs_treats_nested_git_marker_as_boundary` and `test_snapshot_empty_untracked_dirs_treats_nested_git_marker_as_boundary` in `tests/unit/runtime/test_validation_worktree.py`. Both fail before the fix and pass after.

- [x] Focused unit tests for the touched module must pass.
  - **Status:** Complete
  - **Evidence:** `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q` reports `33 passed`.

## Commands run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
# 33 passed

uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py
# Success: no issues found in 1 source file
```

## Files changed

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/NESTED_GIT_WORKTREE_BOUNDARY_PLAN.md`
- `plans/NESTED_GIT_WORKTREE_BOUNDARY_VALIDATION.md`

## Gaps / remaining work

None. All planned requirements are satisfied.
