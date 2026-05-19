# PRRT_kwDOSJAM6s6DTdCV Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6DTdCV` reports that default local service Compose env autodiscovery can walk from `Path.cwd()` up to an unrelated Git root and ingest a foreign `docker/compose/.env` before the AWF module-path fallback runs.

Scope is limited to `src/awf/service/config.py` lookup behavior and focused regression coverage in `tests/unit/service/test_config.py`.

## Requirements Checklist

- Default `docker/compose/.env` autodiscovery from the current working directory must only accept env files associated with verified AWF source roots.
- AWF module-path fallback must continue to find a checked-out AWF source root when commands run outside that checkout.
- Explicit env file paths passed to `local_service_environ(..., env_file=...)` or `resolve_local_service_compose_env_file(env_file=...)` must continue to work without AWF markers.
- Existing host-port derivation behavior must remain covered by tests.

## Implementation Steps

1. Add a regression test proving an unrelated Git repo with `docker/compose/.env` is ignored and the AWF module-path fallback is used instead.
2. Update default current-working-directory search roots to require AWF source-root markers instead of a generic `.git` marker.
3. Adjust existing tests that intentionally exercise default discovery so their temporary roots include AWF source markers.
4. Run the focused test file, then the narrow service-config subset needed to prove the change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  - Passes with the new regression test and existing service config coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py tests/unit/service/test_config.py`
  - Passes without lint regressions.
