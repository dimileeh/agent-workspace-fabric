# Mirror Hooks Recovery Finish Validation

Plan reference: `plans/MIRROR_HOOKS_RECOVERY_FINISH_PLAN.md`

## Requirement Status

- Finish active recovery operations on mirror-hooks repair failure when
  `recovery is not None`: Complete.
- Preserve existing non-recovery failure behavior: Complete.
- Propagate the mirror repair failure reason code to recovery completion and
  workspace failure: Complete.
- Add a focused regression test for the recovery branch: Complete.
- Run targeted checks only: Complete.

## Evidence

- Changed `src/awf/control/executor/execution_flow.py` so the mirror-hooks
  failure branch calls `_finish_active_recovery_operations` with
  `OperationStatus.failed` before `_mark_failed` during recovery resumes.
- Extended `tests/unit/control/test_executor_mirror_hooks_path.py` to cover both
  non-recovery and active recovery mirror repair failure paths.
- Confirmed the test failed before the production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  failed on the recovery parameter because no recovery finish call was made.
- Targeted verification after the fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  passed with `2 passed`.
- Narrow lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py tests/unit/control/test_executor_mirror_hooks_path.py`
  passed.

Full AWF/GitHub validation, coverage gates, and CI-equivalent suites are managed
by AWF after agent completion per the workspace contract.
