# PRRT_kwDOSJAM6s6COw7_ Auth Response Metadata Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6COw7_` reports that newly protected operator
routes now advertise bearer authentication but do not document the shared
API-token 401 and 503 failure envelopes in OpenAPI. Scope is limited to response
metadata and the checked-in OpenAPI artifact for the reported routes:
tasks, locks, merge queue, release readiness, and metrics.

## Requirements Checklist

- [ ] Extend the OpenAPI regression test so the reported protected routes must
      document bearer auth plus 401 and 503 auth failure responses.
- [ ] Confirm the extended regression fails before implementation.
- [ ] Add shared API-token auth error response metadata to the affected routers
      or routes without changing runtime auth behavior.
- [ ] Regenerate `openapi.json`.
- [ ] Run focused OpenAPI validation and relevant static checks.

## Implementation Steps

1. Add the missing task, lock, merge queue, release readiness, and metrics
   operations to the protected-route OpenAPI regression set.
2. Run that test and capture the expected failure against the current code.
3. Import and apply `API_TOKEN_AUTH_ERROR_RESPONSES` in the affected route
   modules.
4. Regenerate the checked-in OpenAPI artifact.
5. Run focused tests and spec-drift validation, then document the result.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated -q`
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes tests/unit/api/test_openapi_artifact.py`
