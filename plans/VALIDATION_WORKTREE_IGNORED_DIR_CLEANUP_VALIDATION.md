# Validation Worktree Ignored Directory Cleanup Validation

Plan reference: `plans/VALIDATION_WORKTREE_IGNORED_DIR_CLEANUP_PLAN.md`

## Requirement status

- Add a regression for generated ignored files leaving empty nested directories:
  Complete. Added
  `test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files`.
- Preserve the ignored root itself and avoid deleting non-empty directories:
  Complete. Added
  `test_cleanup_validation_worktree_preserves_non_empty_ignored_dirs_after_cleaning_new_files`.
- Keep existing ignored-file safety checks intact:
  Complete. Focused cleanup tests covering modified baseline files, deleted
  baseline files, and deleted empty ignored roots pass.
- Keep cleanup failure reporting deterministic when an empty generated directory
  cannot be removed: Complete. Added
  `test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed`.
- Validate with focused local checks only: Complete. Full AWF/GitHub validation
  remains owned by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/VALIDATION_WORKTREE_IGNORED_DIR_CLEANUP_PLAN.md`
- `plans/VALIDATION_WORKTREE_IGNORED_DIR_CLEANUP_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files -q`
  - First run failed before implementation on the stale `.venv/new` directory.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_preserves_non_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed -q`
  - Passed: `3 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_new_ignored_files_using_snapshot tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_preserves_non_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_modified_ignored_file_using_snapshot_signature tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_ignored_snapshot_path_disappears tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_root_disappears -q`
  - Passed: `7 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  - Passed.
- `git diff --check`
  - Passed.

Additional note: I also ran the full touched unit file
`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`.
It failed on four restore-ref expectation tests that reproduce independently of
this change, including
`test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr`.
Those failures are outside this review-thread fix and were not broadened here.
