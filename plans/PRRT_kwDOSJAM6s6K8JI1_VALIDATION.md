# PRRT_kwDOSJAM6s6K8JI1 Validation

## Result

- Complete: Added a focused regression for dirty-finalize
  `_MonitorHeadObjectMissingError` raised by `_commit_dirty_worktree`.
- Complete: Confirmed the regression failed before the source change because the
  result reason code stayed `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`.
- Complete: Added a dirty-finalize catch block that preserves
  `_MonitorHeadObjectMissingError.reason_code` as the returned
  `ValidationWorktreeCheck.reason_code`.

## Evidence

- Red test before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py::test_pre_push_validation_dirty_finalize_preserves_head_object_missing -q`
  failed with `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` instead of
  `HEAD_OBJECT_MISSING_UNRECOVERABLE`.
- Focused pytest after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py -q`
  passed: `2 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py`
  passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
