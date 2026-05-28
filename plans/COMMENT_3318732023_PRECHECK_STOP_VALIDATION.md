# Comment 3318732023 Precheck Stop Validation

Plan reference: `plans/COMMENT_3318732023_PRECHECK_STOP_PLAN.md`

## Requirement Status

- Complete: Required companion env precheck failures still record the existing
  `MONITOR_RECOVERY_PRECHECK_FAILED` runtime restart event.
- Complete: Required companion env precheck failures now return immediately
  after recording the failure, before monitor construction or `monitor.run`.
- Complete: Existing compose restart failure behavior remains unchanged; the
  focused compose failure test still verifies monitor continuation.
- Complete: Focused regression coverage now expects no monitor run for both
  missing and empty required companion env source values.
- Complete: Only targeted local validation was run; broad AWF/GitHub
  validation remains managed after agent completion.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py`
- `plans/COMMENT_3318732023_PRECHECK_STOP_PLAN.md`
- `plans/COMMENT_3318732023_PRECHECK_STOP_VALIDATION.md`

Focused checks:

- Initial expected failure before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py::TestExecutorCoverageEdgesPart010::test_resume_pr_monitor_stops_after_required_companion_env_secret_precheck_failure -q`
  - Result: failed for both missing and empty source cases because
    `monitor_calls` contained the workspace id.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py::TestExecutorCoverageEdgesPart010::test_resume_pr_monitor_stops_after_required_companion_env_secret_precheck_failure -q`
  - Result: passed, `2 passed`.
- Compose failure branch guard:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::TestExecutorCoverageEdgesPart002::test_resume_pr_monitor_compose_failure_records_warning_and_runs_monitor -q`
  - Result: passed, `1 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_010.py`
  - Result: passed.

No full repository test suite, full coverage gate, full frontend build, or
CI-equivalent AWF validation was run in the agent phase.
