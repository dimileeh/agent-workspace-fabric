# PRRT_kwDOSJAM6s6CO3aF Release Readiness 503 Schema Plan

## Problem Statement and Scope

The `/release-readiness` route is API-token protected and can return two distinct 503 bodies:

- auth middleware/runtime failures as `HttpExceptionErrorResponse`
- normal failed release scorecards as the release-readiness report body

The current OpenAPI metadata attaches the shared auth response map directly, so the generated spec documents the route's 503 response only as `HttpExceptionErrorResponse`. Scope is limited to correcting the route's OpenAPI contract and adding a regression test for that documented shape.

## Requirements Checklist

- Preserve bearer-auth documentation and 401 auth error schema for `/release-readiness`.
- Document `/release-readiness` 503 as accepting both auth failure envelopes and failed release-readiness scorecards.
- Avoid changing runtime readiness behavior.
- Update the checked-in OpenAPI artifact if the generated spec changes.
- Validate with the narrowest relevant tests and the OpenAPI drift check.

## Implementation Steps

1. Add explicit Pydantic models for the release-readiness scorecard response shape in `src/awf/api/routes/health.py`.
2. Add a route-local 503 OpenAPI response that uses `anyOf` for `HttpExceptionErrorResponse` and the scorecard model while preserving the shared 401 auth metadata.
3. Add or update OpenAPI regression coverage in `tests/unit/api/test_openapi_artifact.py`.
4. Regenerate `openapi.json`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_health.py -q`
  - Passes with the new schema regression and existing health behavior.
- `python scripts/generate_openapi.py --check`
  - Passes with the checked-in artifact matching generated OpenAPI.
