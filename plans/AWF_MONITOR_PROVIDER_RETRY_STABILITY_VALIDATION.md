# AWF Monitor Provider Retry Stability Validation

## Result

Implemented.

## Evidence

- Reproduced the missing durable state with a failing unit test:
  `test_provider_circuit_breaker_suppresses_monitor_cli_and_records_event_and_state`
  failed with `KeyError: 'provider_recovery_state'` before the fix.
- Added durable monitor retry state when a provider/model circuit suppresses PR
  monitor CLI execution.
- Verified focused behavior:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_provider_circuit_breaker_suppresses_monitor_cli_and_records_event_and_state tests/unit/control/test_worker.py::TestRunOnceMonitorRecovery::test_stale_active_scan_preserves_monitor_provider_retry_cooldown tests/unit/control/test_worker.py::TestRunOnceMonitorRecovery::test_stale_active_scan_preserves_due_monitor_provider_fallback_for_resume -q`
  passed with 3 tests.
- Verified formatting/lint and typing:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner.py`
  passed.
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Remaining Work

Rebuild/restart the local AWF service and attach fresh PR monitors to PR #265
and PR #266.
