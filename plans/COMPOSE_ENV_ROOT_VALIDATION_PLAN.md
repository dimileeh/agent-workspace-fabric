# Compose Env Root Validation Plan

## Problem statement and scope

PR review thread `PRRT_kwDOSJAM6s6DMG0w` reports that service CLI commands can discover
`docker/compose/.env` from an ancestor directory even when AWF cannot verify that ancestor
as a bootstrap asset root. That can pass an unrelated env file to Docker Compose while the
default compose file remains relative to the current working directory.

Scope is limited to service CLI env-file discovery in `src/awf/cli/main.py` and focused
unit coverage in `tests/unit/cli/test_service_cli.py`.

## Requirements checklist

- Add a regression test proving that, when no bootstrap asset root is verified, service
  commands do not pass an ancestor `docker/compose/.env` discovered above the current
  directory.
- Preserve the existing fallback that uses `docker/compose/.env` when it exists under the
  current working directory for non-source local-service runs.
- Preserve verified source-checkout behavior where `get_bootstrap_asset_root()` supplies the
  absolute compose and env paths.
- Keep the change narrowly scoped.

## Implementation steps

1. Add a failing service CLI regression test using `service logs` from a subdirectory under a
   non-verified project that contains an ancestor `docker/compose/.env`.
2. Update `_resolve_existing_local_service_compose_env_file()` so the no-asset-root fallback
   checks only the current working directory's local-service env file, matching compose-file
   resolution.
3. Run the targeted unit tests covering service env-file discovery.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
  - Passes with the new regression and existing service CLI env-file tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_service_cli.py`
  - Passes without lint regressions.
