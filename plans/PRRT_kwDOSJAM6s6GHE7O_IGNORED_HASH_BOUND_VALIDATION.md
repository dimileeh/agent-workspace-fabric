# PRRT_kwDOSJAM6s6GHE7O Ignored Hash Bound Validation

Plan reference: `PRRT_kwDOSJAM6s6GHE7O_IGNORED_HASH_BOUND_PLAN.md`

## Requirement Status

- Bound total ignored regular-file content bytes read during signature capture:
  Complete. `_snapshot_ignored_path_signatures` now carries a total content hash
  budget and falls back to regular-file metadata when the next file would exceed
  the remaining budget.
- Preserve existing ignored path snapshots for added/deleted ignored entries:
  Complete. Snapshot path collection via `git ls-files` and empty directory
  capture is unchanged.
- Preserve content-hash modification detection for small ignored snapshots:
  Complete. Files within the budget still use SHA-256 content signatures, and
  existing modified-ignored-file safety tests pass.
- Fall back to metadata signatures after the content-hash budget is exhausted:
  Complete. A focused regression asserts the first file consumes the budget and
  the next file receives a `metadata:` signature instead of a content hash.
- Add focused regression coverage:
  Complete. Added
  `test_ignored_snapshot_signatures_bound_regular_file_content_hashing`.
- Avoid broad AWF/GitHub-owned validation:
  Complete. Only targeted local checks were run; full AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/PRRT_kwDOSJAM6s6GHE7O_IGNORED_HASH_BOUND_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GHE7O_IGNORED_HASH_BOUND_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_ignored_snapshot_signatures_bound_regular_file_content_hashing -q`
  - Initial run failed before implementation with
    `_snapshot_ignored_path_signatures() got an unexpected keyword argument
    'max_content_hash_bytes'`.
  - Post-implementation run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_hash_file_contents_regular_file_has_stable_digest tests/unit/runtime/test_validation_worktree.py::test_ignored_snapshot_signatures_bound_regular_file_content_hashing tests/unit/runtime/test_validation_worktree.py::test_hash_file_contents_symlink_encodes_target tests/unit/runtime/test_validation_worktree.py::test_hash_file_contents_special_file_encodes_metadata tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_modified_ignored_file_using_snapshot_signature tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_empty_ignored_dir_becomes_file tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_ignored_file_becomes_empty_dir -q`
  - Passed: `7 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py`
  - Passed.
- `git diff --check -- src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  - Passed.

## Gaps And Iteration

The planned full-file command
`uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
was attempted and currently fails four cleanup tests unrelated to this change:

- `test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr`
- `test_cleanup_validation_worktree_ignores_pre_existing_ignored_paths_in_cleanup`
- `test_cleanup_validation_worktree_fails_ignored_snapshot_when_no_stderr`
- `test_cleanup_validation_worktree_marks_untracked_files_as_clean_after_cleanup`

Those failures exercise the existing `restore_ref is None` untracked-cleanup
path and the command tuple/list assertion shape; this review thread is limited
to bounding ignored snapshot content hashing. No further iteration was applied
to that unrelated cleanup behavior in this thread-specific fix.
