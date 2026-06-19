# Validation: Preserve ignored empty directories during validation-worktree cleanup

## Plan reference

`plans/PRESERVE_IGNORED_EMPTY_DIRS_PLAN.md`

## Requirements checklist

- [x] `_remove_empty_untracked_dirs` leaves empty directories that are ignored by `.gitignore` rules even when `git status` does not print them.
- [x] `_snapshot_empty_untracked_dirs` likewise does not surface such directories as dirty (they stay ignored/setup-owned).
- [x] The pre-push validation worktree check (`check_validation_worktree_clean(..., remove_empty_untracked_dirs=True)`) reports clean when the only workspace state is an empty directory matching a wildcard ignore rule such as `cache/**`.
- [x] Non-ignored empty directories continue to be removed when `remove_empty_untracked_dirs=True`.
- [x] Non-empty ignored directories and ignored files continue to be treated according to existing behavior.
- [x] New regression tests cover the wildcard-ignored empty-directory case for both removal and snapshot, and for the public `check_validation_worktree_clean` API with `remove_empty_untracked_dirs=True`.
- [x] Existing regression tests still pass; no existing logic is weakened.

## Evidence

### Files changed

- `src/awf/runtime/validation_worktree.py`
  - Added `_is_ignored_path` helper using `git check-ignore --no-index <path>`.
  - `_remove_empty_untracked_dirs` now skips an empty directory when `_is_ignored_path` reports it is ignored.
  - `_snapshot_empty_untracked_dirs` now treats an ignored empty directory as if it has a file descendant, so it is not surfaced as dirty.
- `tests/unit/runtime/test_validation_worktree.py`
  - Added `_init_real_worktree_with_gitignore` helper to create a real git worktree with a committed `.gitignore`.
  - Added `_run_git_in_real_worktree` to exercise the public async API with real git commands.
  - Added three regression tests:
    - `test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir`
    - `test_snapshot_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir`
    - `test_check_validation_worktree_clean_preserves_wildcard_ignored_empty_dir`
- `plans/PRESERVE_IGNORED_EMPTY_DIRS_PLAN.md` and `plans/PRESERVE_IGNORED_EMPTY_DIRS_VALIDATION.md`

### Commands run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
# 47 passed

uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py -q
# 30 passed

uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py
# All checks passed!

uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py
# Success: no issues found in 1 source file
```

Full AWF/GitHub validation, provenance, logs, coverage gates, and merge gating are managed by AWF after agent completion.

## Gap assessment

No gaps remain. All planned requirements are satisfied.
