# Validation: Guard pre-push validation retries against newly ignored snapshots

## Plan reference
- [pre_push_validation_ignored_snapshot_PLAN.md](pre_push_validation_ignored_snapshot_PLAN.md)

## Validation outcomes
- `check_pre_push_validation_retries_guard` — **Complete**
  - Evidence:
    - `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
    - `_run_pre_push_validation_with_fix_passes` now carries baseline ignored state into subsequent passes.
    - `_run_pre_push_validation` now evaluates `baseline` ignored roots/snapshots before launching validation commands and exits with `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` when gains are detected.

- `avoid_regression_retries_and_fix_behaviors` — **Complete**
  - Evidence:
    - Existing ignored-snapshot retry-path tests updated to accept the revised command-capture behavior.
    - New test: `test_run_pre_push_validation_rejects_new_ignored_entries_before_validation` added.

## Verification commands
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_pre_push_validation.py -k "run_pre_push_validation_rejects_new_ignored_entries_before_validation or fix_pass_uses_initial_ignored_snapshot_across_retries or fix_pass_rejects_new_ignored_paths_on_retry"`
  - Result: `3 passed, 30 deselected`

## Notes
- This is focused validation scoped to the changed behavior and the review-thread fix. No broad AWF/CI validation suite was run locally.
