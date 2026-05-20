# CI Service Logs Compose Env Lookup Plan

## Problem Statement And Scope

PR #264 fails the `python-full-coverage` GitHub Actions job because
`tests/unit/cli/test_service_cli.py::test_service_logs_reuses_resolved_asset_root_for_compose_env_file`
observes two bootstrap asset-root lookups for `awf service logs`. The command
already resolves service compose paths once, but the runtime env-file resolution
path revalidates the compose file through config helpers that rediscover the
bootstrap asset root.

Scope is limited to preserving the existing local-service compose env trust
model while avoiding the redundant asset-root lookup for service commands that
already consumed `_resolve_service_compose_paths()`.

## Requirements Checklist

- Reproduce the focused CI failure locally before changing code.
- Do not weaken compose env-file forwarding for untrusted direct helper calls.
- Make `awf service logs` reuse the resolved asset-root compose paths without a
  second bootstrap asset-root lookup.
- Keep the change scoped to CLI service env resolution and matching tests/docs
  for this fix cycle.
- Validate with the focused failing test and the narrow related CLI init/service
  test surface.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Inspect the failing GitHub Actions job and confirm the failing test.
2. Run the focused failing pytest node locally.
3. Update CLI runtime env resolution so trusted paths from
   `_resolve_service_compose_paths()` are reused without calling the stricter
   rediscovery helper.
4. Preserve `_trusted_service_compose_env_file()` behavior for direct untrusted
   callers and existing tests.
5. Run focused and related tests, plus lint/type checks if the touched surface
   justifies them.
6. Write validation evidence in `plans/CI_SERVICE_LOGS_COMPOSE_ENV_LOOKUP_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_logs_reuses_resolved_asset_root_for_compose_env_file -q`
  - Passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py -q`
  - Passes, or any unrelated failure is documented with evidence.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_service_cli.py tests/unit/cli/test_init.py`
  - Passes.
