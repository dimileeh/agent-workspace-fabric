# Review 4482045018 Service Env Fallback Validation

Plan reference: `plans/REVIEW_4482045018_SERVICE_ENV_FALLBACK_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Preserve conservative `awf init` behavior. | Complete | `_run_init_service_bootstrap()` still calls `_resolve_service_env_files()` without the source-less compose fallback opt-in; `test_service_env_resolution_ignores_current_compose_env_without_asset_root` passed. |
| Restore service-command current-directory compose env fallback. | Complete | `service status`, `doctor`, `readiness`, `bootstrap`, and `logs` now pass `allow_current_compose_env_without_asset_root=True`; all existing no-source service command fallback regressions passed. |
| Preserve the ancestor guard. | Complete | The fallback checks only the current working directory's `docker/compose/.env` and requires the adjacent `local-service.yml`; `test_service_logs_ignores_ancestor_compose_env_without_source_checkout` passed. |
| Preserve root `.env` fallback semantics. | Complete | Root `.env` remains the active settings env file and is still not forwarded as Compose `--env-file`; focused status/logs regressions passed. |
| Keep the change narrowly scoped. | Complete | Changed only `src/awf/cli/main.py`, `tests/unit/cli/test_init.py`, and required plan/validation docs. |

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_init.py`
- `plans/REVIEW_4482045018_SERVICE_ENV_FALLBACK_PLAN.md`
- `plans/REVIEW_4482045018_SERVICE_ENV_FALLBACK_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_service_env_resolution_uses_current_compose_env_when_explicitly_allowed -q`
  - Failed before implementation with `TypeError: _resolve_service_env_files() got an unexpected keyword argument 'allow_current_compose_env_without_asset_root'`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_service_env_resolution_ignores_current_compose_env_without_asset_root tests/unit/cli/test_init.py::test_service_env_resolution_uses_current_compose_env_when_explicitly_allowed tests/unit/cli/test_service_cli.py::test_service_status_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_readiness_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_bootstrap_cli_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_doctor_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_logs_uses_existing_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_logs_ignores_ancestor_compose_env_without_source_checkout tests/unit/cli/test_service_cli.py::test_service_status_resolves_settings_from_existing_root_env tests/unit/cli/test_service_cli.py::test_service_logs_passes_source_checkout_compose_env_file tests/unit/cli/test_service_cli.py::test_service_logs_omits_root_env_file_when_compose_env_is_missing -q`
  - Passed: `11 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
  - Passed: `75 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  - Passed: `77 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed: no issues found in 155 source files.

## Remaining Gaps

None.
