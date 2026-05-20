# Review 4482045018 Service Logs Path Reuse Plan

## Problem Statement And Scope

PR #264 review comment `issue:4482045018` reports unresolved service logs test
failures around Compose path resolution. The exact named path assertion is
green locally, but the surrounding logs slice still fails because service logs
resolve the bootstrap asset root twice: once in `_resolve_service_compose_paths`
and again while deciding whether the Compose env file is trusted.

Scope is limited to preserving the verified Compose path context across service
CLI runtime env resolution without weakening the existing guard that rejects
unrelated `docker/compose/.env` files.

## Requirements Checklist

- Preserve existing regression tests and assertions.
- Keep unverified direct `_resolve_service_runtime_env_files` calls guarded by
  `_is_local_service_compose_file_path`.
- Avoid a second `get_bootstrap_asset_root()` lookup for service CLI commands
  that just consumed `_resolve_service_compose_paths()`.
- Preserve root `.env` fallback behavior when the Compose-specific `.env` is
  absent.
- Validate the service logs slice and the related CLI init guard tests.
- Commit only files changed for this review comment fix.

## Implementation Steps

1. Confirm the service logs failure locally with the narrow test slice.
2. Extend `_resolve_service_runtime_env_files` with an explicit verified-paths
   mode for callers that just used `_resolve_service_compose_paths()`.
3. Update service CLI call sites to pass verified-path context after resolving
   Compose paths centrally.
4. Keep the default runtime env resolver behavior unchanged for direct/private
   tests and unverified callers.
5. Run targeted tests and record validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q -k service_logs`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k "trusted_service_compose_env_file or service_runtime_env_resolution or service_env_resolution or service_compose_env_file"`

Pass criteria: both targeted commands pass, and the validation document records
the observed pre-fix failure plus post-fix evidence.
