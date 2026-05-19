# PRRT_kwDOSJAM6s6DRZFy Compose DB URL Explicitness Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DRZFy_PLAN.md`

## Requirement Status

- Add a failing unit test for a sourced project `.env` default
  `AWF_DATABASE_URL` plus compose-only `AWF_POSTGRES_HOST_PORT`: Complete.
  Evidence:
  `test_service_settings_sourced_env_default_database_url_uses_compose_env_postgres_host_port`.
- Use the merged service environment when deciding whether the default host
  database URL can be derived from the Compose Postgres port: Complete.
  Evidence: `_database_url_env_is_explicit()` now receives `service_env` and
  treats a host-exported stock URL matching the project `.env` as derivable
  when the merged Compose environment carries `AWF_POSTGRES_HOST_PORT`.
- Preserve existing explicit host/default and constructor/default regression
  tests: Complete.
  Evidence: the focused regression group including
  `test_service_settings_host_default_database_url_ignores_compose_env_postgres_host_port`
  and `test_service_settings_explicit_default_database_url_ignores_postgres_host_port_override`
  passed.
- Run focused service config tests and lint for touched files: Complete.

## Evidence

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_service_settings_sourced_env_default_database_url_uses_compose_env_postgres_host_port -q`
  failed with `localhost:5433` instead of `localhost:15433`.
- Focused regression group after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_service_settings_sourced_env_default_database_url_uses_compose_env_postgres_host_port tests/unit/service/test_config.py::test_service_settings_host_default_database_url_ignores_compose_env_postgres_host_port tests/unit/service/test_config.py::test_service_settings_exported_default_database_url_uses_postgres_host_port_override tests/unit/service/test_config.py::test_service_settings_explicit_default_database_url_ignores_postgres_host_port_override -q`
  passed, 4 tests.
- Service config unit file:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  passed, 94 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py tests/unit/service/test_config.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf/service/config.py`
  passed.

## Gaps

None.
