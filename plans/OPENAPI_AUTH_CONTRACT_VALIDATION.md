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
- Complete: Regenerate `openapi.json` and verify it has no spec drift. Evidence:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- Complete: Run the narrowest relevant unit checks for dependency behavior and
  the OpenAPI contract. Evidence: focused unit tests, Ruff, and mypy passed.

## Files Changed

- `src/awf/api/deps.py`
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

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated -q`
  failed with `AssertionError: assert None == {'scheme': 'bearer', 'type': 'http'}`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_deps.py -q`
  passed: `18 passed`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed: `OK: openapi.json matches the current app spec.`
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passed:
  `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf` passed:
  `Success: no issues found in 154 source files`.

## Notes

The bare command `python scripts/generate_openapi.py` failed in this workspace
because the global interpreter lacks FastAPI. The same script succeeded under
`uv run --python 3.12 --extra dev`, which supplies the repository's dev
dependencies.
