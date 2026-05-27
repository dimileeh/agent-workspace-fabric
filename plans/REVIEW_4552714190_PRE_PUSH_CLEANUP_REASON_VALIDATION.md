# Review 4552714190 Pre-Push Cleanup Reason Validation

Plan reference:
`plans/REVIEW_4552714190_PRE_PUSH_CLEANUP_REASON_PLAN.md`

## Requirement Status

- Complete: Updated the cleanup-error regression to require
  `PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED` on the returned push result.
- Complete: Preserved the failed validation run's raw compose cleanup reason
  code, asserted as `EXEC_PROCESS_CLEANUP_FAILED`.
- Complete: Kept the push blocked when cleanup fails.
- Complete: Avoided broad AWF/GitHub-owned validation; only focused tests and
  focused lint were run.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `plans/REVIEW_4552714190_PRE_PUSH_CLEANUP_REASON_PLAN.md`
- `plans/REVIEW_4552714190_PRE_PUSH_CLEANUP_REASON_VALIDATION.md`

Commands run:

- Failing first, as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_cleanup_error_records_failed_run -q`
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_cleanup_error_records_failed_run -q`
- Passing focused regression surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_remote_ops.py -q`
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_remote_ops.py`

Full AWF/GitHub validation was not run inside the agent phase; AWF owns the
broad validation, provenance, logs, timeouts, and merge gating after this
focused fix.
