# CI Service Logs Compose Env Lookup Validation

Plan reference: `plans/CI_SERVICE_LOGS_COMPOSE_ENV_LOOKUP_PLAN.md`

## Requirement Status

- Complete: Reproduced the focused CI failure locally before changing code.
  - Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_reuses_resolved_asset_root_for_compose_env_file -q`
    failed with `assert 2 == 1`.
- Complete: Did not weaken compose env-file forwarding for untrusted direct
  helper calls.
  - Evidence: `_trusted_service_compose_env_file()` remains unchanged for
    direct rediscovery/revalidation callers.
- Complete: `awf service logs` reuses resolved asset-root compose paths without
  a second bootstrap asset-root lookup.
  - Evidence: `src/awf/cli/main.py` now uses
    `_trusted_resolved_service_compose_env_file()` in
    `_resolve_service_runtime_env_files()`.
- Complete: Change is scoped to CLI service env resolution and plan/validation
  docs.
  - Evidence: changed files are `src/awf/cli/main.py` and the CI plan docs.
- Complete: Focused and related tests pass.
  - Evidence:
    - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_reuses_resolved_asset_root_for_compose_env_file -q`
      passed: `1 passed`.
    - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py -q`
      passed: `198 passed`.
    - `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py`
      passed.
    - `uv run --python 3.12 --extra dev mypy src/awf`
      passed: `Success: no issues found in 158 source files`.
- Complete: Commit locally without pushing or switching branches.
  - Evidence: this validation file is staged with the fix for the local commit.

## Residual Risk

The full `python-full-coverage` job was not rerun locally because the focused
failure reproduced and the related CLI unit surface passed. CI will rerun the
full coverage gate after AWF pushes this local commit.
