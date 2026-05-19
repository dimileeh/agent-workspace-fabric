# PRRT_kwDOSJAM6s6DSaBO Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DSaBO_PLAN.md`

## Requirement Status

- Add a regression test proving an exported default `AWF_DATABASE_URL` sourced
  from the module-path checkout `.env` is treated as derivable when compose
  `AWF_POSTGRES_HOST_PORT` is present: Complete.
- Add a regression test proving the same behavior for `AWF_API_BASE_URL` and
  compose `AWF_API_HOST_PORT`: Complete.
- Preserve existing cwd-based `.env` behavior and explicit host URL handling:
  Complete.
- Keep the fix scoped to `src/awf/service/config.py` and focused unit tests:
  Complete.

## Evidence

- Changed `src/awf/service/config.py` so `_project_dotenv_value()` reads from
  the project `.env` associated with the resolved default Compose env file,
  including module-path fallback resolution.
- Changed `tests/unit/service/test_config.py` to cover module-path fallback
  sourced defaults for both database and API URL derivation.
- Confirmed the new regressions failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_module_path_sourced_default_database_url_uses_compose_postgres_host_port tests/unit/service/test_config.py::test_module_path_sourced_default_api_base_url_uses_compose_api_host_port -q`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_module_path_sourced_default_database_url_uses_compose_postgres_host_port tests/unit/service/test_config.py::test_module_path_sourced_default_api_base_url_uses_compose_api_host_port -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py tests/unit/service/test_config.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf/service/config.py`

## Gaps

None.
