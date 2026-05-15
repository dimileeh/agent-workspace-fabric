# PRRT_kwDOSJAM6s6CO3aF Release Readiness 503 Schema Validation

Plan reference: `PRRT_kwDOSJAM6s6CO3aF_RELEASE_READINESS_503_SCHEMA_PLAN.md`

## Requirement Status

- Preserve bearer-auth documentation and 401 auth error schema for `/release-readiness`: Complete.
  - The route still uses `Depends(require_api_token)` and preserves the shared 401 auth metadata.
- Document `/release-readiness` 503 as accepting both auth failure envelopes and failed release-readiness scorecards: Complete.
  - `src/awf/api/routes/health.py` now uses a route-specific 503 response model union.
  - `tests/unit/api/test_openapi_artifact.py` asserts the 503 schema references both `HttpExceptionErrorResponse` and `ReleaseReadinessResponse`.
- Avoid changing runtime readiness behavior: Complete.
  - Existing `/release-readiness` behavior tests still pass, including the failed scorecard returning HTTP 503 with `status == "fail"`.
- Update the checked-in OpenAPI artifact if the generated spec changes: Complete.
  - `openapi.json` was regenerated with the new release-readiness response schemas.
- Validate with the narrowest relevant tests and the OpenAPI drift check: Complete.

## Evidence

- Changed files:
  - `src/awf/api/routes/health.py`
  - `tests/unit/api/test_openapi_artifact.py`
  - `openapi.json`
- Commands run:
  - `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/health.py tests/unit/api/test_openapi_artifact.py`
  - `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_health.py -q`
  - `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
