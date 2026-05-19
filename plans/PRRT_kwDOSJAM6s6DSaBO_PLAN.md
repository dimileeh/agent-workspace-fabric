# PRRT_kwDOSJAM6s6DSaBO Plan

## Problem Statement And Scope

The unresolved review thread reports that sourced-default detection for
`AWF_DATABASE_URL` and `AWF_API_BASE_URL` only checks `.env` from the current
working directory. When `docker/compose/.env` is resolved from the AWF module
path fallback, commands launched outside the checkout can read compose host-port
overrides but fail to recognize that exported default URLs came from the
checkout `.env`. Those defaults are then treated as explicit and stale default
ports are kept.

Scope is limited to service config resolution and regression tests for the
module-path fallback case.

## Requirements Checklist

- Add a regression test proving an exported default `AWF_DATABASE_URL` sourced
  from the module-path checkout `.env` is treated as derivable when compose
  `AWF_POSTGRES_HOST_PORT` is present.
- Add a regression test proving the same behavior for `AWF_API_BASE_URL` and
  compose `AWF_API_HOST_PORT`.
- Preserve existing cwd-based `.env` behavior and explicit host URL handling.
- Keep the fix scoped to `src/awf/service/config.py` and focused unit tests.

## Implementation Steps

1. Add failing tests in `tests/unit/service/test_config.py` that simulate a
   command running outside the checkout while `resolve_local_service_compose_env_file()`
   finds compose `.env` through the module path fallback.
2. Update project `.env` lookup so it includes the project root associated with
   the resolved default compose env file, not only cwd ancestors.
3. Re-run the focused failing tests, then the narrow service config unit tests.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6DSaBO_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::<new-test-name> -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  passes.
