# Validation: Fix Gitlink Lookup Failure Removing Submodules (PRRT_kwDOSJAM6s6KHcyk)

## Plan Reference

`plans/PRRT_kwDOSJAM6s6KHcyk_PLAN.md`

## Requirement-by-Requirement Status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | `_gitlink_paths` must not return an empty set on `git ls-tree` failure; it must surface the failure so callers cannot silently proceed as if there are no submodules. | Complete | `src/awf/runtime/validation_worktree.py`: `_gitlink_paths` now raises `_GitlinkLookupError` when `result.returncode != 0`. |
| 2 | `check_validation_worktree_clean` (pre-push path, `remove_empty_untracked_dirs=True`) must report the worktree as dirty when the gitlink lookup fails, not silently remove submodule directories. | Complete | `src/awf/runtime/validation_worktree.py`: the empty-directory cleanup/snapshot block is wrapped in `try/except _GitlinkLookupError` and returns `clean=False` with `VALIDATION_WORKTREE_STATUS_FAILED`. |
| 3 | `_remove_empty_untracked_dirs` and `_snapshot_empty_untracked_dirs` must not accept a stale/failed gitlink lookup; their existing boundary checks must remain correct. | Complete | Boundary logic unchanged; both helpers still call `_gitlink_paths` and now propagate the exception. |
| 4 | Add a regression test proving that when `_gitlink_paths` fails, empty-directory cleanup does not remove a deinitialized tracked submodule and the clean check reports dirty. | Complete | `tests/unit/runtime/test_validation_worktree.py`: added `test_check_validation_worktree_clean_fails_when_gitlink_lookup_fails`. |
| 5 | Ensure existing regression tests for tracked deinitialized submodules still pass. | Complete | `test_remove_empty_untracked_dirs_preserves_tracked_deinitialized_submodule`, `test_snapshot_empty_untracked_dirs_preserves_tracked_deinitialized_submodule`, and related tests pass. |

## Evidence

### Files Changed

- `src/awf/runtime/validation_worktree.py`
  - Added `_GitlinkLookupError` exception class.
  - Changed `_gitlink_paths` to raise on `git ls-tree` failure instead of returning `frozenset()`.
  - Wrapped `_remove_empty_untracked_dirs` / `_snapshot_empty_untracked_dirs` calls in `check_validation_worktree_clean` with `try/except _GitlinkLookupError`, returning a dirty check with `VALIDATION_WORKTREE_STATUS_FAILED`.
- `tests/unit/runtime/test_validation_worktree.py`
  - Added regression test `test_check_validation_worktree_clean_fails_when_gitlink_lookup_fails`.
  - Updated `_init_fake_worktree` and several existing tests to use real git control directories so the new gitlink lookup succeeds during tests that are not exercising the failure path.
- `plans/PRRT_kwDOSJAM6s6KHcyk_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6KHcyk_VALIDATION.md`

### Tests Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
```

Result:

```
42 passed in 1.50s
```

### Lint/Type Run

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py
```

Result:

```
All checks passed!
Success: no issues found in 1 source file
```

## Conclusion

All planned requirements are complete. The review-thread issue (PRRT_kwDOSJAM6s6KHcyk) is fixed: a failed `git ls-tree` gitlink lookup can no longer silently allow removal of deinitialized tracked submodules or cause pre-push validation to report a false-clean worktree.
