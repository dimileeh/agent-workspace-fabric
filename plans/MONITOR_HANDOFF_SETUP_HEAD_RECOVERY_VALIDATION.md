# Monitor Handoff Setup HEAD Recovery Validation

Plan reference: `plans/MONITOR_HANDOFF_SETUP_HEAD_RECOVERY_PLAN.md`

## Requirement Status

- Verify whether the review comment is actionable against local code: Complete.
  The local `ComposeExecCleanupError` handler in `monitor_handoff_setup.py` repaired mirror hooks and marked the workspace failed without HEAD verification/recovery.
- On monitor handoff setup cleanup failure, check whether the worktree HEAD object exists before marking the cleanup failure: Complete.
  The cleanup failure path now calls `verify_head_object_exists(worktree_path)`.
- If HEAD is missing and the executor exposes `_recover_missing_git_head_or_mark_failed`, attempt filesystem recovery with `mark_failed_on_failure=False`: Complete.
  The helper loads persisted workspace metadata when needed and calls the existing executor recovery hook with `mark_failed_on_failure=False`.
- Preserve the existing cleanup failure classification and final `_mark_failed` behavior: Complete.
  The code still marks the cleanup failure with `EXEC_PROCESS_CLEANUP_FAILED` after the recovery attempt.
- Add focused regression coverage for the cleanup failure recovery call and arguments: Complete.
  Added `test_handoff_setup_cleanup_failure_recovers_missing_head_before_mark_failed`.
- Run only targeted validation; leave broad AWF/GitHub validation to AWF after agent completion: Complete.
  No broad suite, full coverage gate, frontend build, push, or branch operation was run.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff_setup.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_018.py`
- `plans/MONITOR_HANDOFF_SETUP_HEAD_RECOVERY_PLAN.md`
- `plans/MONITOR_HANDOFF_SETUP_HEAD_RECOVERY_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_018.py -q`
  Result: `10 passed in 2.33s`
- `uv run --python 3.12 --extra dev ruff format src/awf/control/executor/monitor_handoff_setup.py`
  Result: `1 file reformatted`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_018.py`
  Result: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff_setup.py`
  Result: `Success: no issues found in 1 source file`

Full AWF/GitHub validation is intentionally not run in the agent phase; AWF owns broad validation and merge gating after agent completion.
