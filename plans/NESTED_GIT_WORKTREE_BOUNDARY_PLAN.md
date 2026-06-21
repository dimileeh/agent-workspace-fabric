# Plan: Stop recursing into nested Git worktrees

## Problem statement and scope

GitHub PR review thread `PRRT_kwDOSJAM6s6KCVkh` on `src/awf/runtime/validation_worktree.py:207` reports that `_remove_empty_untracked_dirs` recurses into nested Git repositories/worktrees/submodules before checking for a `.git` marker inside the child. The marker only prevents removing that child's root after its empty descendants may already have been `rmdir`'d. In the superproject case, `git status --porcelain --untracked-files=all --ignored=matching` reports an empty untracked directory inside a submodule as clean, so the PR monitor could silently delete `sub/empty/` while still allowing the push.

The same traversal issue exists in `_snapshot_empty_untracked_dirs`, which also walks into children before checking for `.git`. The fix must treat any directory containing a `.git` marker as a traversal boundary **before** recursing.

Scope is limited to the two traversal helpers in `src/awf/runtime/validation_worktree.py` and the corresponding unit tests in `tests/unit/runtime/test_validation_worktree.py`.

## Explicit requirements checklist

- [ ] `_remove_empty_untracked_dirs` must not recurse into a directory that contains a `.git` marker; it must treat such a directory as a non-empty descendant and leave it alone.
- [ ] `_snapshot_empty_untracked_dirs` must not recurse into a directory that contains a `.git` marker; it must treat such a directory as a file-descendant boundary (i.e. a non-empty signal) so its empty descendants are not snapshot.
- [ ] The `.git` marker check must happen **before** the recursive call, not after.
- [ ] Existing behavior for regular empty directories, ignored roots, symlinks, non-empty directories, and filesystem edges must remain unchanged.
- [ ] Regression tests must cover both helpers with a nested `.git` directory boundary.
- [ ] Focused unit tests for the touched module must pass.

## Implementation steps

1. Add regression tests that create a worktree containing a nested directory with its own `.git` marker and an empty descendant inside it. Assert the nested empty descendant is not removed by `_remove_empty_untracked_dirs` and is not reported by `_snapshot_empty_untracked_dirs`.
2. In `_remove_empty_untracked_dirs`, move the `child.name == ".git"` check to the top of the loop and, when present, mark the directory as having a descendant and continue (do not recurse).
3. In `_snapshot_empty_untracked_dirs`, similarly move the `.git` check to the top of the loop and treat the directory as having a file descendant, continuing without recursion.
4. Run the focused unit tests and verify.

## Verification commands and pass criteria

Run the narrow unit-test command for the touched module:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
```

Pass criteria:
- All tests in `tests/unit/runtime/test_validation_worktree.py` pass.
- The new regression tests fail before the fix and pass after the fix.
- `ruff` and `mypy` checks on the modified source file still pass (optional narrow lint/type check if practical).
