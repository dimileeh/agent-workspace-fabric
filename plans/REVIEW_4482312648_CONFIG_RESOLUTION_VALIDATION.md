# Review 4482312648 Config Resolution Validation

Plan reference: `plans/REVIEW_4482312648_CONFIG_RESOLUTION_PLAN.md`

## Requirement Status

- Preserve explicit constructor `database_url` and `api_base_url` values with
  custom service `environ` port overrides: Complete.
- Stop storing `_awf_init_fields` through Pydantic `PrivateAttr` dual storage:
  Complete.
- Find default `docker/compose/.env` inside the current checkout/project root
  from nested working directories: Complete.
- Avoid matching default `docker/compose/.env` above an installed module path
  unless the module path is inside a recognizable AWF source root: Complete.
- Keep explicit/absolute env-file paths working as before: Complete.

## Evidence

Files changed:

- `src/awf/common/config.py`
- `src/awf/service/config.py`
- `tests/unit/service/test_config.py`

Regression tests added:

- `test_settings_constructor_fields_are_not_pydantic_private_dual_storage`
- `test_default_compose_env_lookup_ignores_unmarked_module_ancestor`
- `test_default_compose_env_lookup_accepts_awf_project_root_from_module_path`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  - Initial run failed with the two new regressions before implementation.
  - Final run passed: `82 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_config_uses_postgres_default_and_redacts_secrets tests/unit/cli/test_service_cli.py::test_service_mode_uses_postgres_when_database_env_unset tests/unit/cli/test_service_cli.py::test_service_mode_preserves_explicit_postgres_url -q`
  - Passed: `3 passed`.

Additional note:

- `uv run --python 3.12 --extra dev pytest tests/unit -q` was attempted, but
  it remained early in the suite after several minutes and was stopped. The
  focused config and CLI service tests cover the changed behavior directly.

## Gaps

None for the saved plan.
