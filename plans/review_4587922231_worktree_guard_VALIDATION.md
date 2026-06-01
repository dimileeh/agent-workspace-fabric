# Validation: Address review comment 4587922231 worktree guard cleanup

## Plan reference
- [review_4587922231_worktree_guard_PLAN.md](review_4587922231_worktree_guard_PLAN.md)

## Requirement status
- `executor_baseline_after_early_exits` — **Complete**
  - `src/awf/control/executor/execution_validation.py` now evaluates pre-existing dirty worktree and missing-HEAD exits before assigning or comparing the setup ignored-snapshot baseline.

- `pre_push_helper_drift_name` — **Complete**
  - `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` renames the helper to `_pre_push_validation_ignored_entries_drifted` and updates the caller variable accordingly.
  - Direct helper tests in `tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py` were renamed to match the drift semantics.

- `current_signature_lookup_normalized` — **Complete**
  - `src/awf/runtime/validation_worktree.py` now builds the current ignored-signature lookup through `_ignored_signature_lookup_by_normalized_path`, matching the baseline lookup behavior.

- `focused_regression_coverage` — **Complete**
  - Added `test_cleanup_validation_worktree_normalizes_current_signature_lookup`, which failed before the source fix and passes after the normalized lookup change.

- `focused_local_validation_only` — **Complete**
  - Ran targeted unit and lint checks only. Full AWF/GitHub validation, coverage gates, and broad CI-equivalent commands are left to AWF after agent completion.

## Verification commands
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_validation_worktree.py -k "normalizes_current_signature_lookup or modified_ignored_file_using_snapshot_signature or empty_ignored_dir_becomes_file or ignored_file_becomes_empty_dir"`
  - Result: `4 passed, 35 deselected`

- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py -k "ignored_entries_drifted or run_pre_push_validation_rejects_new_ignored_entries_before_validation"`
  - Result: `4 passed, 18 deselected`

- `uv run --python 3.12 --extra dev pytest -q tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -k "missing_head or dirty_worktree or ignored_paths_after_initial_validation_pass or ignored_signature_drift"`
  - Result: `3 passed, 13 deselected`

- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py`
  - Result: `All checks passed!`

## Notes
- No broad repository validation, full coverage gate, frontend build, push, rebase, or branch switch was run in the agent phase.
