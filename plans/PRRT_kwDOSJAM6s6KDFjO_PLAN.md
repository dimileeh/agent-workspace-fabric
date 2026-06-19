# PRRT_kwDOSJAM6s6KDFjO Plan: Batch gitlink checks for validation worktree

## Problem statement

The review thread at `src/awf/runtime/validation_worktree.py:317` points out that
`_snapshot_empty_untracked_dirs` and `_remove_empty_untracked_dirs` call
`_is_tracked_gitlink` for every directory they visit before deciding whether that
subtree is empty or a removal candidate. `_is_tracked_gitlink` shells out to
`git ls-tree HEAD -- <path>`, so a large monorepo with no submodules can spend
thousands of process launches per cleanliness check.

## Scope

- Load tracked gitlink paths once per traversal instead of shelling out per
  directory.
- Defer gitlink detection until a directory is actually an empty removal/reporting
  candidate (for `_remove_empty_untracked_dirs` / `_snapshot_empty_untracked_dirs`).
- Keep existing submodule-preservation behavior unchanged.
- Add focused regression tests proving a single `git ls-tree` call per traversal.
- Do not refactor broader worktree cleanup logic or unrelated git calls.

## Requirements checklist

1. `_is_tracked_gitlink` is no longer invoked per directory during traversal.
2. Gitlink set is loaded once per traversal via `git ls-tree -r -d HEAD`.
3. Existing submodule-preservation behavior is unchanged for real submodules.
4. New regression test fails before the fix and passes after.
5. Existing tests in `tests/unit/runtime/test_validation_worktree.py` still pass.
6. `ruff check` and `mypy` pass for touched files.

## Implementation steps

1. Add `_gitlink_paths(worktree_path: Path) -> frozenset[str]` helper that runs
   `git ls-tree -r -d HEAD` and returns the set of paths with mode `160000`.
2. Modify `_remove_empty_untracked_dirs` to call `_gitlink_paths` once and pass
   the set into `maybe_remove_empty`, checking membership instead of shelling out.
3. Modify `_snapshot_empty_untracked_dirs` the same way.
4. Keep `_is_tracked_gitlink` unchanged for callers outside the traversal.
5. Add a unit test that creates a real git worktree with a deinitialized submodule,
   monkeypatches `subprocess.run`, and asserts exactly one `git ls-tree` call is
   made during snapshot and one during removal.
6. Run targeted tests and checks.
7. Write `PRRT_kwDOSJAM6s6KDFjO_VALIDATION.md`.

## Verification commands

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
```

All must pass.
