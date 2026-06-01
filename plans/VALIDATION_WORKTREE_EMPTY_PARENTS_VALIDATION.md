# Validation Worktree Empty Parents Validation

Plan reference: `plans/VALIDATION_WORKTREE_EMPTY_PARENTS_PLAN.md`

## Requirement Status

- Complete: Added a regression for an untracked generated file under a new
  non-ignored directory where cleanup removes only the file path.
- Complete: Added deepest-first removal of empty non-ignored parent directories
  after successful untracked cleanup.
- Complete: Kept ignored-root cleanup on the existing path and skipped
  non-ignored parent cleanup for paths under ignored roots.
- Complete: Empty parent removal failures return
  `VALIDATION_WORKTREE_CLEANUP_FAILED` with `cleanup_command="rmdir"`.
- Complete: Ran focused validation only; full AWF/GitHub validation is managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/VALIDATION_WORKTREE_EMPTY_PARENTS_PLAN.md`
- `plans/VALIDATION_WORKTREE_EMPTY_PARENTS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_untracked_parent_after_file_cleanup -q`
  - First run before implementation: failed because `gen/` remained as an empty
    untracked directory.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_marks_untracked_files_as_clean_after_cleanup tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_untracked_parent_after_file_cleanup tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_for_untracked_dirty_state_when_restore_ref_missing -q`
  - Passed: `3 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  - Passed: `49 passed`.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  - Passed.

## Notes

The validation-worktree module had nearby stale tests that expected untracked
cleanup without `restore_ref`, conflicting with the existing dedicated
missing-restore-ref regression. Those tests were updated to exercise the
supported restore-ref cleanup path without weakening the missing-restore-ref
safety check.
