# Review 4482045018 Service Env Follow-up Plan

## Problem Statement And Scope

Address the follow-up review feedback from PR #264 comment `issue:4482045018`
for local service Compose env handling. The scope is limited to the reported
environment helper behaviors:

- avoid building a Docker logs subprocess environment only to restate caller
  Compose selector variables;
- surface malformed Compose YAML while collecting interpolation keys;
- make root `.env` pairing for `docker/compose/.env` symlink-aware;
- ensure `_local_service_asset_path` honors the verified asset-root contract for
  absolute paths.

## Requirements Checklist

- Add or update focused regression tests before implementation.
- Preserve explicit service-provided Compose selector overrides.
- Preserve explicit blank Compose selector values that clear stale caller values.
- Do not pass a subprocess `env` for `docker compose logs` when the only relevant
  values are inherited caller `COMPOSE_*` selectors.
- Raise a parse failure for malformed Compose YAML and still re-read the file
  after its contents change.
- Resolve symlinks before deriving a paired root `.env` from a Compose env path.
- Reject absolute local-service asset paths outside the verified asset root.
- Commit only the files changed for this review comment follow-up.

## Implementation Steps

1. Add regression tests in the existing unit test modules for service logs,
   CLI init env path resolution, and service config path resolution.
2. Run the narrow tests to confirm they fail against the current implementation
   where practical.
3. Update `src/awf/service/environment.py` so silent service Compose selector
   keys inherit from the caller without making the override dict non-empty, and
   malformed YAML parse errors propagate.
4. Update `src/awf/cli/main.py` so `_compose_root_env_file` performs the
   structural check on the resolved path.
5. Update `src/awf/service/config.py` so `_local_service_asset_path` rejects
   absolute paths outside the verified asset root.
6. Run targeted unit tests, then lint/type checks if the narrow tests pass.
7. Record validation evidence in
   `plans/REVIEW_4482045018_SERVICE_ENV_FOLLOWUP_VALIDATION.md` and commit the
   scoped changes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_init.py tests/unit/service/test_config.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: targeted tests pass, lint and type checks pass, and validation
documents each planned requirement as complete or explicitly deferred.
