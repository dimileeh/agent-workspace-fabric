# PRRT_kwDOSJAM6s6GC842 Ignored Baseline HEAD Rollback Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GC842_IGNORED_HEAD_ROLLBACK_PLAN.md`

## Requirement Status

- Regression test for deleted ignored snapshot plus moved HEAD: Complete.
  Added
  `test_cleanup_validation_worktree_rolls_back_head_when_deleted_ignored_snapshot_fails`.
- Preserve ignored-baseline cleanup failure classification: Complete. The new
  path still returns `VALIDATION_WORKTREE_CLEANUP_FAILED`.
- Avoid cleaning or restoring deleted/modified pre-existing ignored snapshot
  files: Complete. The regression asserts no `git clean` is issued for the
  deleted baseline file.
- Keep validation local and focused: Complete. No full coverage, full unit
  suite, frontend build, or AWF/GitHub-owned broad validation was run.

## Evidence

Files changed:

- `src/awf/runtime/validation_worktree.py`
- `tests/unit/runtime/test_validation_worktree.py`
- `plans/PRRT_kwDOSJAM6s6GC842_IGNORED_HEAD_ROLLBACK_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GC842_IGNORED_HEAD_ROLLBACK_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_deleted_ignored_snapshot_fails -q`
  failed before implementation, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_deleted_ignored_snapshot_fails -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  was attempted after implementation and failed in four existing unrelated
  no-`restore_ref`/assertion tests outside this review thread's scope.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_when_ignored_snapshot_path_disappears tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_rolls_back_head_when_deleted_ignored_snapshot_fails tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_fails_modified_ignored_file_using_snapshot_signature tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_new_ignored_files_using_snapshot -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py`
  passed.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.

## Iteration 1

The only validation gap was the planned full module command, which failed in
pre-existing tests unrelated to ignored-baseline HEAD rollback. The highest
impact scoped iteration was to verify the exact affected cleanup paths and lint
the touched files; those checks pass.
