# PRRT_kwDOSJAM6s6GGahq Literal Clean Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GGahq_LITERAL_CLEAN_PLAN.md`

## Requirement Status

- Regression for generated ignored pathspec metacharacters: Complete.
  Added
  `test_cleanup_validation_worktree_cleans_generated_ignored_metachar_path_literally`,
  which uses a real temporary Git repository to prove `.venv/foo[1]` is cleaned
  without deleting preserved baseline `.venv/foo1`.
- Literal `git clean` semantics: Complete.
  Validation cleanup now invokes `git --literal-pathspecs clean -fdx -- ...`
  for generated untracked cleanup paths.
- Preserve cleanup failure behavior, reason codes, and HEAD rollback checks:
  Complete for the touched cleanup path. Existing exact-command tests were
  updated to assert the literal pathspec command shape, and targeted rollback
  and ignored-cleanup tests pass.
- Focused validation only: Complete.
  No full AWF/GitHub-owned validation or coverage gate was run.

## Evidence

- Changed files:
  - `src/awf/runtime/validation_worktree.py`
  - `tests/unit/runtime/test_validation_worktree.py`
  - `plans/PRRT_kwDOSJAM6s6GGahq_LITERAL_CLEAN_PLAN.md`
  - `plans/PRRT_kwDOSJAM6s6GGahq_LITERAL_CLEAN_VALIDATION.md`
- First failing regression run before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_generated_ignored_metachar_path_literally -q`
  failed because preserved baseline `.venv/foo1` was removed.
- Final regression run:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_generated_ignored_metachar_path_literally -q`
  passed.
- Focused cleanup tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_clean_fails tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_ignored_files_with_none_stderr tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_new_ignored_files_using_snapshot tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_generated_ignored_metachar_path_literally tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_empty_ignored_dirs_after_cleaning_new_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_removes_new_empty_ignored_dirs_without_files tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_detects_head_change_after_dirty_cleanup -q`
  passed.
- Lint/format/type checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passed.
  `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passed.
  `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  passed.
- Exploratory broader focused selection:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q -k 'clean or ignored'`
  failed with four out-of-scope existing failures:
  `test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr`,
  `test_cleanup_validation_worktree_ignores_pre_existing_ignored_paths_in_cleanup`,
  `test_cleanup_validation_worktree_fails_ignored_snapshot_when_no_stderr`, and
  `test_cleanup_validation_worktree_marks_untracked_files_as_clean_after_cleanup`.
  These failures relate to existing restore-ref guard expectations and a
  list-vs-tuple command assertion, not to the literal pathspec change.

## Remaining Gaps

None for this review thread. Broad AWF/GitHub validation remains owned by AWF
after agent completion.
