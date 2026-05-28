# Companion Env Secret Resume Precheck Validation

Plan reference: `plans/COMPANION_ENV_SECRET_RESUME_PRECHECK_PLAN.md`

## Requirement Status

- Complete: Added `CompanionEnvSecretPrecheckError` for monitor-resume companion env-secret precheck failures.
- Complete: `_precheck_required_companion_env_secrets_for_resume` raises the dedicated precheck exception for missing or empty required env-backed companion secrets.
- Complete: `resume_pr_monitor` catches precheck failures before generic `ComposeOperationError` and logs `executor.resume_companion_env_secret_precheck_failed`.
- Complete: Existing `ComposeOperationError` handling remains in place for actual Compose failures.
- Complete: Required-secret reason codes and diagnostic stderr are preserved without raw secret values.
- Complete: The persisted monitor runtime restart event now records `MONITOR_RECOVERY_PRECHECK_FAILED` and `operation=companion_env_secret_precheck` for AWF precheck failures.
- Complete: Focused regression coverage verifies the dedicated precheck log path and guards that real Compose failures still use the Compose path.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "required_companion_env_secret_reason_code"`
  - Expected red before implementation: failed because `executor.resume_companion_env_secret_precheck_failed` was not emitted.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "required_companion_env_secret_reason_code or compose_failure_records_warning"`
  - Passed: `3 passed, 13 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py`
  - Passed.

Full AWF/GitHub validation is intentionally left to AWF after agent completion per the workspace contract.
