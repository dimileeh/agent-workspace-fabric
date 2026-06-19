# Plan: Preserve ignored empty directories during validation-worktree cleanup

## Problem statement and scope

PR #606 review thread `PRRT_kwDOSJAM6s6KH8Na` (src/awf/runtime/validation_worktree.py:603) reports that `git status --porcelain=v1 --untracked-files=all --ignored=matching` does not emit entries for empty directories that are ignored by wildcard patterns such as `cache/**`. When the pre-push cleanliness check runs with `remove_empty_untracked_dirs=True`, `_remove_empty_untracked_dirs` receives only the ignored paths that git status explicitly printed plus the AWF runtime roots. Because the empty `cache/` directory is absent from that set, the helper removes it and reports the worktree clean. That mutates setup-owned/ignored workspace state during an otherwise clean push.

Scope is limited to the validation-worktree empty-directory helpers and their callers:

- `src/awf/runtime/validation_worktree.py` (owned)
- `tests/unit/runtime/test_validation_worktree.py` (owned)
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py` and its cleanup variant if they exercise the pre-push path (owned)

## Requirements checklist

- [ ] `_remove_empty_untracked_dirs` leaves empty directories that are ignored by `.gitignore` rules even when `git status` does not print them.
- [ ] `_snapshot_empty_untracked_dirs` likewise does not surface such directories as dirty (they stay ignored/setup-owned).
- [ ] The pre-push validation worktree check (`check_validation_worktree_clean(..., remove_empty_untracked_dirs=True)`) reports clean when the only workspace state is an empty directory matching a wildcard ignore rule such as `cache/**`.
- [ ] Non-ignored empty directories continue to be removed when `remove_empty_untracked_dirs=True`.
- [ ] Non-empty ignored directories and ignored files continue to be treated according to existing behavior.
- [ ] New regression tests cover the wildcard-ignored empty-directory case for both removal and snapshot, and for the public `check_validation_worktree_clean` API with `remove_empty_untracked_dirs=True`.
- [ ] Existing regression tests still pass; no existing logic is weakened.

## Implementation steps

1. Add a helper `_is_ignored_by_git(worktree_path, relative_dir)` that invokes `git check-ignore --no-index <path>` and returns True when the directory matches an ignore rule. Use a subprocess call with the same `git_safe_directory_config_args` used elsewhere.
2. In `_remove_empty_untracked_dirs`, when a candidate directory is empty and otherwise removable, call `_is_ignored_by_git` and skip removal (treat as `had_descendant`) if it is ignored.
3. In `_snapshot_empty_untracked_dirs`, when a directory has no file descendants and would be added to `empty_dirs`, call `_is_ignored_by_git` and skip adding it if it is ignored.
4. Add unit tests:
   - `_remove_empty_untracked_dirs` with a real git repo containing `.gitignore` `cache/**` and an empty `cache/` directory.
   - `_snapshot_empty_untracked_dirs` with the same setup; returns empty tuple.
   - `check_validation_worktree_clean(..., ignore_all_ignored=True, remove_empty_untracked_dirs=True)` reports clean and preserves `cache/`.
5. Optionally add a focused test in the pre-push cleanup test file if the owned path has a suitable harness; otherwise rely on the validation-worktree tests since the pre-push runner delegates to this helper.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py` passes.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py` passes.
- (Focused) `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py -q` passes if those files are touched.
