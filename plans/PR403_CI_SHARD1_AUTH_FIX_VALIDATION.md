# PR403 CI Shard 1 Auth Fix Validation

## Result

The CI shard 1 failure was reproduced locally and fixed.

Root cause:

- `_api_token_headers()` fell back to `local_service_environ()` when
  `AWF_API_TOKEN` was unset.
- Because `local_service_environ()` now fills the raw Compose local default,
  ordinary CLI requests sent `Bearer local-dev-token`.
- `TestWorkspaceObservability.test_runtime_fetches_without_token_header_when_unset`
  expected no authorization header and failed in CI.

Fix:

- Generic CLI auth now uses only explicit `--api-token` or process
  `AWF_API_TOKEN`.
- Service-aware commands that intentionally resolve local service settings still
  pass their resolved token explicitly.
- Mocked CLI HTTP tests now isolate `local_service_environ()` from a developer's
  root `.env`, so local `AWF_API_HOST_PORT` values cannot change expected URLs.

## Validation

Initially failed locally:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestWorkspaceObservability::test_runtime_fetches_without_token_header_when_unset -q
```

Passed after the fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestWorkspaceObservability::test_runtime_fetches_without_token_header_when_unset tests/unit/cli/test_common_helpers.py::test_api_token_headers_do_not_fall_back_to_local_compose_default tests/unit/cli/test_common_helpers.py::test_api_token_headers_preserve_explicit_and_shell_precedence tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_minimal_development_payload tests/unit/cli/test_service_gc_cli.py::test_service_gc_loads_service_env_token_when_base_url_in_process_env tests/unit/cli/test_service_gc_cli.py::test_service_gc_skips_service_env_when_overrides_present -q
```

Passed broader touched set:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_service_gc_cli.py tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py -q
```
