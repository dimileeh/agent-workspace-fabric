# Compose Env Root Validation

Plan reference: `plans/COMPOSE_ENV_ROOT_VALIDATION_PLAN.md`

## Requirement status

- Add a regression test proving that no unverified ancestor `docker/compose/.env` is passed:
  Complete. Added `test_service_logs_ignores_ancestor_compose_env_without_source_checkout`.
- Preserve the current-directory non-source fallback:
  Complete. Existing non-source service CLI tests still pass.
- Preserve verified source-checkout behavior:
  Complete. Existing service CLI source-checkout tests remain covered by the full service CLI test file.
- Keep the change narrowly scoped:
  Complete. The implementation only changes compose env-file discovery in `src/awf/cli/main.py`.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_service_cli.py`
- `plans/COMPOSE_ENV_ROOT_VALIDATION_PLAN.md`
- `plans/COMPOSE_ENV_ROOT_VALIDATION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_ignores_ancestor_compose_env_without_source_checkout -q`
  - Failed before the implementation, proving the regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_ignores_ancestor_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_logs_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_bootstrap_cli_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_status_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_readiness_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_doctor_uses_existing_compose_env_without_source_checkout -q`
  - Passed: 6 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
  - Passed: 75 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_service_cli.py`
  - Passed.

## Remaining gaps

None.
