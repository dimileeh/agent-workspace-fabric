# Plan: Fix Gitlink Lookup Failure Removing Submodules (PRRT_kwDOSJAM6s6KHcyk)

## Problem Statement

`src/awf/runtime/validation_worktree.py` function `_gitlink_paths` currently returns an empty set when its `git ls-tree -z -r -d HEAD` subprocess fails. When this happens during pre-push validation's empty-untracked-directory cleanup, `_remove_empty_untracked_dirs` no longer recognizes a deinitialized tracked submodule as a traversal boundary, so it may `rmdir` the empty submodule directory. The cleanliness decision is made before the removal, using the earlier `git status` output that showed no change, so validation can report a clean tree while the worktree is actually dirty.

This is a genuine correctness bug: a failed infrastructure probe (git ls-tree) silently downgrades safety, allowing tracked state to be destroyed and validation to lie about cleanliness.

## Scope

Only the `_gitlink_paths` helper and its callers in `src/awf/runtime/validation_worktree.py`, plus focused unit tests in `tests/unit/runtime/test_validation_worktree.py`. The PRD-quoted review locations are line 226 (`_gitlink_paths` return-empty branch) and lines 561-571 (`_remove_empty_untracked_dirs` call site inside `check_validation_worktree_clean`).

## Requirements Checklist

1. `_gitlink_paths` must not return an empty set on `git ls-tree` failure; it must surface the failure so callers cannot silently proceed as if there are no submodules.
2. `check_validation_worktree_clean` (the pre-push path, with `remove_empty_untracked_dirs=True`) must report the worktree as dirty when the gitlink lookup fails, not silently remove submodule directories.
3. `_remove_empty_untracked_dirs` and `_snapshot_empty_untracked_dirs` must not accept a stale/failed gitlink lookup; their existing boundary checks must remain correct.
4. Add a regression test proving that when `_gitlink_paths` fails, empty-directory cleanup does not remove a deinitialized tracked submodule and the clean check reports dirty.
5. Ensure existing regression tests for tracked deinitialized submodules still pass.

## Implementation Steps

1. Change `_gitlink_paths` signature/behavior so failure is an exception rather than an empty set. Introduce a small private exception (`_GitlinkLookupError`) carrying the subprocess stderr/reason, and raise it when `result.returncode != 0`.
2. Wrap calls to `_gitlink_paths` inside `check_validation_worktree_clean` with a `try/except _GitlinkLookupError`. On failure return a `ValidationWorktreeCheck` with `clean=False` and reason code `VALIDATION_WORKTREE_STATUS_FAILED` (or `VALIDATION_INFRASTRUCTURE_ERROR`), so pre-push validation cannot pass.
3. Keep `_remove_empty_untracked_dirs` and `_snapshot_empty_untracked_dirs` unmodified in their boundary logic; they already depend on `_gitlink_paths` returning the real set.
4. Update existing unit tests that may monkeypatch `subprocess.run` to simulate ls-tree failure; verify the new behavior, and add a new regression test for the pre-push clean-check path.
5. Run the narrow focused tests (`pytest tests/unit/runtime/test_validation_worktree.py -q`) and lint/type check on touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py` passes.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py` passes.
- Full repository coverage gate is left to AWF/GitHub CI per workspace contract; do not run it locally.
