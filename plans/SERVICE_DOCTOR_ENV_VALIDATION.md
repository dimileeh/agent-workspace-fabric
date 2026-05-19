# Service Doctor Env Validation

Plan reference: `SERVICE_DOCTOR_ENV_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `awf service doctor` resolves settings and provider environment from the resolved compose env file.
  - Evidence: `tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_compose_env`
- Complete: Updated `service_doctor` to use `_resolve_service_env_paths()`, `local_service_environ(env_file=...)`, and `resolve_service_settings(Settings(_env_file=...), environ=...)`.
  - Evidence: `src/awf/cli/main.py`
- Complete: Preserved `service_doctor --bundle` behavior by keeping support bundle collection on the same resolved settings and environment variables used by diagnostics.
  - Evidence: `src/awf/cli/main.py`
- Complete: Added coverage proving `_init_env_warning()` uses already display-ready payload values directly.
  - Evidence: `tests/unit/cli/test_init.py::test_init_env_warning_uses_display_ready_payload_paths`
- Complete: Ran focused and broader validation for the changed CLI behavior.
  - Evidence: commands listed below.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_doctor_resolves_settings_from_compose_env tests/unit/cli/test_init.py::test_init_env_warning_uses_display_ready_payload_paths -q`
  - First run: failed as expected before implementation.
  - Final run: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py -q`
  - Passed: `121 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py src/awf/cli/main.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.
