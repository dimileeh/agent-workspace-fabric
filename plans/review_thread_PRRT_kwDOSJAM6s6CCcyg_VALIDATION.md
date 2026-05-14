# Review Thread PRRT_kwDOSJAM6s6CCcyg Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CCcyg_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving preserved-active-execution
  restart recovery advances `Workspace.version` exactly once.
- Complete: Preserved the existing preservation event payload, operation
  creation, subphase update, and stale event expectations in the same test.
- Complete: Removed the redundant manual version bump from
  `ControlWorker._record_preserved_active_execution_after_restart`.
- Complete: Ran the focused failing-before/passing-after unit test and narrow
  lint check for touched Python files.

## Evidence

- Failing-before evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_restart_recovery_preserves_live_validating_and_pushing_runtimes -q`
  failed before the worker change because version advanced by two for both
  validating and pushing preservation cases.
- Passing-after evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_restart_recovery_preserves_live_validating_and_pushing_runtimes -q`
  passed.
- Passing-after evidence:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.

## Files Changed

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/review_thread_PRRT_kwDOSJAM6s6CCcyg_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6CCcyg_VALIDATION.md`
