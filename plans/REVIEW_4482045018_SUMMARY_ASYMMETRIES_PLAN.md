# Plan: Address Review 4482045018 Summary Asymmetries

## Problem Statement And Scope

Greptile's review-level comment on PR #264 calls out remaining asymmetries in
local service Docker environment handling and bootstrap compose asset
resolution. The fix should address the real hazards without weakening existing
regression tests that intentionally allow `service logs` to use a resolved
`DOCKER_HOST` service environment value when `AWF_DOCKER_HOST` is absent.

Scope is limited to:

- `src/awf/service/logs.py` Docker CLI environment construction.
- `src/awf/service/bootstrap.py` default compose asset detection.
- Focused unit regressions in `tests/unit/service/test_logs.py` and
  `tests/unit/service/test_bootstrap.py`.

## Requirements Checklist

- Add a regression proving `service logs` removes stale caller Docker host
  case variants when `AWF_DOCKER_HOST` supplies the Docker client host.
- Add a regression proving `service logs` removes stale caller Docker host
  case variants when the resolved service environment supplies `DOCKER_HOST`.
- Preserve the existing tested behavior where `DOCKER_HOST` from the resolved
  service environment is honored when `AWF_DOCKER_HOST` is absent.
- Add a regression proving bootstrap treats an absolute path to the local
  service compose file under the resolved asset root as the default compose
  asset, not as a custom user compose path.
- Keep changes scoped and avoid branch switches, pushes, or destructive git
  operations.

## Implementation Steps

1. Add failing unit tests for the logs Docker host scrubbing cases and bootstrap
   absolute default compose asset detection.
2. Run the narrow tests to confirm the new regressions fail before code changes
   where practical.
3. Update `logs._docker_cli_environ` to scrub `AWF_DOCKER_HOST`,
   `DOCKER_CONTEXT`, and all pre-existing `DOCKER_HOST` case variants before
   writing the canonical `DOCKER_HOST` override whenever a Docker host is
   resolved.
4. Update `bootstrap._resolve_bootstrap_assets` so an absolute compose file
   matching `asset_root / LOCAL_SERVICE_COMPOSE_FILE` is handled by the default
   asset branch.
5. Run focused tests for the changed areas, then create a validation document.
6. Stage only changed files and commit locally with a review-comment-specific
   conventional commit message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/service/test_bootstrap.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/service/bootstrap.py tests/unit/service/test_logs.py tests/unit/service/test_bootstrap.py`
  must pass.
