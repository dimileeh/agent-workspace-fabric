# Review 4482045018 Service Env Follow-up Validation

Plan reference: `plans/REVIEW_4482045018_SERVICE_ENV_FOLLOWUP_PLAN.md`

## Requirement Status

- Add or update focused regression tests before implementation: Complete.
  Added focused regressions in `tests/unit/service/test_logs.py`,
  `tests/unit/cli/test_init.py`, and `tests/unit/service/test_config.py`.
- Preserve explicit service-provided Compose selector overrides: Complete.
  Existing service logs tests covering explicit `COMPOSE_*` values still pass.
- Preserve explicit blank Compose selector values that clear stale caller values:
  Complete. Existing blank-clear tests still pass.
- Do not pass a subprocess `env` for inherited-only caller Compose selectors:
  Complete. Covered by
  `test_service_logs_inherits_caller_compose_cli_vars_without_subprocess_env`.
- Raise a parse failure for malformed Compose YAML and still re-read after
  contents change: Complete. Covered by
  `test_service_logs_surfaces_malformed_compose_yaml_and_reloads_after_fix`.
- Resolve symlinks before deriving a paired root `.env` from a Compose env path:
  Complete. Covered by
  `test_compose_root_env_file_uses_resolved_path_for_symlinked_env_file`.
- Reject absolute local-service asset paths outside the verified asset root:
  Complete. Covered by
  `test_local_service_asset_path_rejects_absolute_path_outside_asset_root`.
- Commit only the files changed for this review comment follow-up: Complete.
  Final commit scope is limited to the follow-up plan, validation, source
  helpers, and regression tests.

## Evidence

- Initial targeted regression run failed for all five new/changed tests before
  implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_inherits_caller_compose_cli_vars_without_subprocess_env tests/unit/service/test_logs.py::test_compose_cli_environ_omits_caller_values_for_absent_service_keys tests/unit/service/test_logs.py::test_service_logs_surfaces_malformed_compose_yaml_and_reloads_after_fix tests/unit/cli/test_init.py::test_compose_root_env_file_uses_resolved_path_for_symlinked_env_file tests/unit/service/test_config.py::test_local_service_asset_path_rejects_absolute_path_outside_asset_root -q`
  passed: 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_init.py tests/unit/service/test_config.py -q`
  passed: 287 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.
