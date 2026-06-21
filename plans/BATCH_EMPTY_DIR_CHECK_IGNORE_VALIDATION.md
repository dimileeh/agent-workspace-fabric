# Batch Empty Directory Check-Ignore Validation

Plan reference: `plans/BATCH_EMPTY_DIR_CHECK_IGNORE_PLAN.md`

## Requirement Status

- Preserve existing cleanup behavior for empty untracked directories: Complete.
  - Evidence: `test_remove_empty_untracked_dirs_batch_check_ignore_candidates` still verifies five empty directory candidates are removed.
- Preserve ignored-directory protection, including wildcard-ignored empty directories: Complete.
  - Evidence: `test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir` passes.
- Preserve failure behavior: if `git check-ignore` fails unexpectedly, do not remove any candidate directories: Complete.
  - Evidence: `test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails` and `test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir_when_check_ignore_fails` pass.
- Use one `git check-ignore --stdin` probe for the cleanup candidate batch: Complete.
  - Evidence: `test_remove_empty_untracked_dirs_batch_check_ignore_candidates` asserts one `check-ignore` call with `--stdin`, `-z`, and batched NUL-delimited input.
- Add focused regression coverage for the batched cleanup behavior: Complete.
  - Evidence: Added `test_remove_empty_untracked_dirs_batch_check_ignore_candidates`.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_batch_check_ignore_candidates -q`
  - Failed before implementation with 5 `check-ignore` calls instead of 1.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_batch_check_ignore_candidates tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir_when_check_ignore_fails tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir -q`
  - Passed: 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_fails_when_check_ignore_fails -q`
  - Passed: 1 test.
- `uv run --python 3.12 --extra dev ruff format tests/unit/runtime/test_validation_worktree.py`
  - Reformatted the edited test file after the commit hook reported formatting drift.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_does_not_partially_clean_when_check_ignore_fails tests/unit/runtime/test_validation_worktree.py::test_remove_empty_untracked_dirs_batch_check_ignore_candidates tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir_when_check_ignore_fails tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_remove_empty_untracked_dirs_preserves_wildcard_ignored_empty_dir tests/unit/runtime/test_validation_worktree_wildcard_ignored.py::test_check_validation_worktree_clean_fails_when_check_ignore_fails -q`
  - Passed after formatting: 5 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase; AWF manages broad validation, provenance, logs, and merge gating after agent completion.
