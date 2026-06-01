# Empty Non-Ignored Directory Guard Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GHUOk_EMPTY_NON_IGNORED_DIR_PLAN.md`

## Requirement Status

- Add a regression test proving an empty non-ignored directory is treated as
  validation worktree dirt even when git status reports no paths: Complete.
- Add a regression test proving cleanup removes a validation-created empty
  non-ignored directory before reporting success: Complete.
- Preserve existing ignored-directory snapshot and cleanup behavior: Complete
  for the focused ignored-directory cleanup slice.
- Run only focused tests and checks for the touched validation worktree code:
  Complete.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/PRRT_kwDOSJAM6s6GHUOk_EMPTY_NON_IGNORED_DIR_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GHUOk_EMPTY_NON_IGNORED_DIR_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_empty_untracked_dirs_as_dirty tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_untracked_dir_after_validation -q`
  - Failed before implementation: 2 failed, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_empty_untracked_dirs_as_dirty tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_untracked_dir_after_validation -q`
  - Passed after implementation: 2 passed.
- `uv run --python 3.12 --extra dev ruff format src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Reformatted the runtime file after the local commit hook's format check.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_handles_none_stdout_as_clean tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_untracked_paths_as_dirty tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_empty_untracked_dirs_as_dirty tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_treats_ignored_paths_as_dirty tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_snapshots_empty_ignored_dirs tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_untracked_dir_after_validation tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_new_empty_ignored_dirs_without_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_preserves_baseline_empty_ignored_dirs tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed -q`
  - Passed: 10 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.

## Gaps

No gaps remain for thread `PRRT_kwDOSJAM6s6GHUOk`.
