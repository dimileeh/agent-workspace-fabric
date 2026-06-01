# Empty Ignored Directory Cleanup Validation

Plan reference:
`plans/review_PRRT_kwDOSJAM6s6GFbv8_empty_ignored_dirs_PLAN.md`

## Requirement Status

- Add a regression test proving cleanup removes a new empty directory under a
  preserved ignored root even when no generated file is reported by
  `git ls-files`: Complete.
- Preserve pre-existing ignored files and directories represented by the
  pre-validation snapshot: Complete.
- Keep deletion/modified-file safety behavior for baseline ignored state:
  Complete for the touched cleanup path; existing safety tests in the focused
  slice still pass.
- Run only targeted tests for the changed validation worktree behavior:
  Complete.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/review_PRRT_kwDOSJAM6s6GFbv8_empty_ignored_dirs_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6GFbv8_empty_ignored_dirs_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_new_empty_ignored_dirs_without_files -q`
  - Failed before implementation, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_new_empty_ignored_dirs_without_files -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_check_validation_worktree_clean_snapshots_empty_ignored_dirs tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_new_empty_ignored_dirs_without_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_preserves_baseline_empty_ignored_dirs tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_preserves_non_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_dir_cannot_be_removed -q`
  - Passed: 6 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  - Failed with four focused-file failures unrelated to this thread:
    cleanup without `restore_ref` expectations and a list-vs-tuple
    assertion in `test_cleanup_validation_worktree_fails_ignored_snapshot_when_no_stderr`.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.

## Gaps

No gaps remain for thread `PRRT_kwDOSJAM6s6GFbv8`.
