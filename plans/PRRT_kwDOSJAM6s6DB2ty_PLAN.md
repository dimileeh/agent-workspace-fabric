# PRRT_kwDOSJAM6s6DB2ty Plan

## Problem Statement and Scope

The PR review reports that `awf init` resolves the correct AWF asset-root compose
environment, but `run_service_bootstrap()` first loads a cwd-relative
`docker/compose/.env` and only overlays the init-provided environment. Values
present only in the cwd env can leak into Docker Compose as host env overrides.

Scope is limited to the local service bootstrap environment resolution and a
focused regression test for the stale cwd env overlay.

## Requirements Checklist

- Add a regression test showing a cwd `docker/compose/.env` value does not leak
  into bootstrap when bootstrap assets resolve to another checkout.
- Preserve the existing partial `provider_environ` overlay behavior.
- Make bootstrap's base environment come from the resolved compose env location,
  not from an unrelated cwd env file.
- Run the narrow unit test surface that proves the fix.

## Implementation Steps

1. Add a failing test in `tests/unit/service/test_bootstrap.py` for stale cwd env
   leakage when an asset-root env is resolved.
2. Update `src/awf/service/bootstrap.py` so bootstrap resolves assets before
   building the service environment and loads the base environment from the
   resolved compose env path.
3. Keep `provider_environ` as an overlay on that resolved base environment.
4. Re-run the focused tests and update validation evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q`
  must pass.
