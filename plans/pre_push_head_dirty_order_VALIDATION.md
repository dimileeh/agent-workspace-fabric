# Pre-Push Head Dirty Order Validation

Plan reference: `plans/pre_push_head_dirty_order_PLAN.md`

## Requirement Status

- Add a regression test for a dirty pre-push validation worktree where `HEAD` capture fails: Complete.
- Ensure dirty worktree detection runs before any failure from `HEAD` capture can mask it: Complete.
- Preserve `PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED` when the worktree is clean but `HEAD` capture fails: Complete.
- Commit the fix locally on the current AWF-managed branch: Complete.

## Evidence

- Changed `tests/unit/runtime/test_pr_monitor_pre_push_validation.py` to cover dirty pre-push worktree classification when `_rev_parse_head` returns `None`.
- Changed `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` so a missing `HEAD` no longer returns before the pre-validation worktree check can classify dirty state.
- Confirmed the new regression failed before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_reports_dirty_worktree_when_head_capture_fails -q`
- Confirmed focused tests pass after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_reports_dirty_worktree_when_head_capture_fails tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_pre_existing_dirty_blocks_before_validation -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_missing_head_blocks_push -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`

Full AWF/GitHub validation is intentionally left to the AWF post-agent and CI phases per the workspace contract.
