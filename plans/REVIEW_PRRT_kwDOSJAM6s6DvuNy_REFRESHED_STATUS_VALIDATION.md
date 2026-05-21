# Review PRRT_kwDOSJAM6s6DvuNy Refreshed Status Validation

Plan: `REVIEW_PRRT_kwDOSJAM6s6DvuNy_REFRESHED_STATUS_PLAN.md`

## Requirement Status

- Complete: Reproduced the stale candidate status problem with a unit test.
  - Evidence: `test_non_running_validation_rewind_uses_refreshed_status_for_salvage_checks`
    failed before the production fix because running salvage events fell
    through to validation redispatch fallback.
- Complete: After a committed validation rewind and refresh, use the refreshed
  workspace status for subsequent recovery decisions.
  - Evidence: `src/awf/control/worker.py` now replaces the effective candidate
    status with the refreshed active workspace status after the committed
    rewind, while preserving the pre-rewind status as the event-floor anchor.
- Complete: Do not change branch management or push behavior.
  - Evidence: No branch or push commands were run.
- Complete: Validate with the narrow worker unit test target.
  - Evidence: Targeted regression and nearby recovery tests passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_non_running_validation_rewind_uses_refreshed_status_for_salvage_checks -q`
  - Before fix: failed as expected.
  - After fix: `3 passed in 10.92s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_non_running_candidate_redispatches_active_validation_recovery_rewinds_to_running tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_non_running_active_validation_fallthrough_refreshes_expiring_session tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_non_running_validation_rewind_uses_refreshed_status_for_salvage_checks tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_validating_candidate_redispatches_active_running_validation_recovery -q`
  - Result: `7 passed in 18.82s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery -q`
  - Result: `142 passed in 224.03s`.

## Remaining Gaps

None.
