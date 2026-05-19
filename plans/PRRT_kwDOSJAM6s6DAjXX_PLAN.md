# PRRT_kwDOSJAM6s6DAjXX Plan

## Problem Statement and Scope

The review thread reports that `awf init` can seed the resolved AWF Compose env
file from a subdirectory, but bootstrap readiness still reads
`docker/compose/.env` relative to the subdirectory. This can omit newly seeded
provider credentials such as `AWF_GITHUB_TOKEN` during strict provider polling.

Scope is limited to the `awf init` local service bootstrap path and its unit
coverage.

## Requirements Checklist

- Add a regression test proving `awf init` passes the resolved seeded Compose
  env file values into bootstrap provider readiness when invoked from a
  subdirectory.
- Preserve existing host-environment override behavior.
- Do not print seeded secret values.
- Keep the behavior scoped to bootstrap mode; project onboarding mode must
  remain unchanged.
- Run focused CLI init tests that cover the change.

## Implementation Steps

1. Add a failing unit test in `tests/unit/cli/test_init.py` using the existing
   bootstrap-mode stubs.
2. Update `_run_init_service_bootstrap` so the env file resolved for seeding is
   also used to build the `provider_environ` passed to `run_service_bootstrap`.
3. Run the focused test, then the relevant `tests/unit/cli/test_init.py` surface.
4. Record validation in `plans/PRRT_kwDOSJAM6s6DAjXX_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  passes.
- The new regression fails before the implementation and passes after it.
