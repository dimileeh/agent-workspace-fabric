# Review 4482045018 Service Env Fallback Plan

## Problem Statement and Scope

The top-level review comment for PR #264 calls out residual risk in service
env-file fallback logic. Focused regression validation found a real failure:
service commands no longer read an existing current-directory
`docker/compose/.env` when no verified source checkout is available, even
though existing service CLI regressions require that fallback for non-source
local-service runs.

Scope is limited to service env-file resolution in `src/awf/cli/main.py`, unit
coverage for the fallback contract, and this plan/validation pair.

## Requirements Checklist

- Preserve conservative `awf init` behavior: without a verified asset root,
  `awf init` must not implicitly use a current-directory compose env file.
- Restore service-command behavior: `awf service readiness`, `logs`,
  `bootstrap`, `status`, and `doctor` must use the current-directory
  `docker/compose/.env` when that compose env and its local service compose file
  exist and no root `.env` is present.
- Preserve the existing ancestor guard: no-asset-root service commands must not
  pass an ancestor `docker/compose/.env` discovered above the current working
  directory.
- Preserve root `.env` fallback semantics: root `.env` may be used for settings
  but must not be forwarded to Docker Compose as `--env-file`.
- Keep the change narrowly scoped and avoid weakening unrelated regressions.

## Implementation Steps

1. Add a focused helper-level regression showing that current-directory compose
   env fallback is available only when explicitly allowed.
2. Thread an explicit opt-in through service commands that need the non-source
   local-service fallback.
3. Keep init using the default conservative helper behavior.
4. Run focused failing/passing tests around current-directory fallback,
   ancestor fallback, root env fallback, and source-checkout compose env
   propagation.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest <focused service env tests> -q`
  - New helper regression fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py -q`
  - Passes, proving all service commands agree on env-file propagation.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_service_env_resolution_ignores_current_compose_env_without_asset_root -q`
  - Passes, proving init/default resolution remains conservative.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py`
  - Passes without lint regressions.
