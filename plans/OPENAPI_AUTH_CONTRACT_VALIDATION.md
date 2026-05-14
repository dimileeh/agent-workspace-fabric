# OpenAPI Auth Contract Validation

Plan reference: `plans/OPENAPI_AUTH_CONTRACT_PLAN.md`

## Requirement Status

- Complete: Add a regression test that fails while API-token-protected REST
  operations are documented as optional authorization-header parameters.
  Evidence: `tests/unit/api/test_openapi_artifact.py` adds
  `test_api_token_routes_are_documented_as_bearer_authenticated`. Before the
  implementation it failed because `components.securitySchemes.bearerAuth` was
  missing.
- Complete: Document protected REST operations with a reusable bearer security
  scheme. Evidence: `src/awf/api/deps.py` now uses a named FastAPI
  `HTTPBearer` security dependency with scheme name `bearerAuth`.
- Complete: Remove the generated optional `authorization` header parameter from
  protected operations by modeling auth through FastAPI security utilities.
  Evidence: regenerated `openapi.json` includes bearer `security` entries and
  no `authorization` header parameter for the protected REST operations covered
  by the regression test.
- Complete: Ensure protected REST operations advertise both 401 Unauthorized and
  503 Service Unavailable error responses using `ErrorResponse`. Evidence:
  protected routers in `workspaces.py`, `artifacts.py`, `logs.py`,
  `operations.py`, and `controls.py` declare 401/503 response metadata.
- Complete: Ensure documented 401 Unauthorized responses expose the
  `WWW-Authenticate` bearer challenge header emitted by `require_api_token`.
  Evidence: `src/awf/api/responses.py` centralizes the 401 response metadata,
  protected routers/routes use it, `tests/unit/api/test_openapi_artifact.py`
  asserts the header for every protected REST operation, and regenerated
  `openapi.json` includes the header.
- Complete: Add runtime auth regression coverage for `require_api_token`
  through the named `HTTPBearer` dependency. Evidence:
  `test_api_token_runtime_failures_match_documented_contract` verifies missing
  and wrong tokens return 401 with the bearer challenge, unset `AWF_API_TOKEN`
  returns 503, and `/healthz` remains public.
- Complete: Regenerate `openapi.json` and verify it has no spec drift. Evidence:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- Complete: Run the narrowest relevant unit checks for dependency behavior and
  the OpenAPI contract. Evidence: focused unit tests, Ruff, and mypy passed.

## Files Changed

- `src/awf/api/deps.py`
- `src/awf/api/responses.py`
- `src/awf/api/routes/artifacts.py`
- `src/awf/api/routes/controls.py`
- `src/awf/api/routes/logs.py`
- `src/awf/api/routes/operations.py`
- `src/awf/api/routes/workspaces.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`
- `plans/OPENAPI_AUTH_CONTRACT_PLAN.md`
- `plans/OPENAPI_AUTH_CONTRACT_VALIDATION.md`

## Commands Run

- Original red check: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated -q`
  failed with `AssertionError: assert None == {'scheme': 'bearer', 'type': 'http'}`.
- Red check for review fix: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated -q`
  failed because 401 responses did not include `headers.WWW-Authenticate`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_deps.py tests/unit/api/test_health.py::test_healthz_does_not_require_auth -q`
  passed: `21 passed`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed: `OK: openapi.json matches the current app spec.`
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed:
  `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf` passed:
  `Success: no issues found in 155 source files`.

## Notes

The bare command `python scripts/generate_openapi.py` failed in this workspace
because the global interpreter lacks FastAPI. The same script succeeded under
`uv run --python 3.12 --extra dev`, which supplies the repository's dev
dependencies.
