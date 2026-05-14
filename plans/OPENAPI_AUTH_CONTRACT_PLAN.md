# OpenAPI Auth Contract Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CMvfI` reports that sensitive AWF REST routes
enforce the local API token at runtime, but the generated OpenAPI contract
documents an optional `authorization` header instead of an authenticated bearer
security requirement. Several affected operations also omit explicit 401 and
503 responses for invalid or unconfigured API-token behavior.

Scope is limited to the OpenAPI/auth contract for existing API-token-protected
routes. Runtime token validation semantics must remain unchanged.

## Requirements Checklist

- [ ] Add a regression test that fails while API-token-protected REST operations
      are documented as optional authorization-header parameters.
- [ ] Document protected REST operations with a reusable bearer security scheme.
- [ ] Remove the generated optional `authorization` header parameter from those
      protected operations by modeling auth through FastAPI security utilities.
- [ ] Ensure protected REST operations advertise both 401 Unauthorized and 503
      Service Unavailable error responses using `ErrorResponse`.
- [ ] Regenerate `openapi.json` and verify it has no spec drift.
- [ ] Run the narrowest relevant unit checks for dependency behavior and the
      OpenAPI contract.

## Implementation Steps

1. Add an OpenAPI regression test covering the bearer security scheme,
   per-operation security, absence of the optional authorization header
   parameter, and 401/503 responses.
2. Run the new test to confirm it fails against the current contract.
3. Change `require_api_token` to use a named `HTTPBearer` security dependency
   while preserving direct-call compatibility for existing unit tests.
4. Add 401/503 response metadata to API-token-protected REST routers and routes.
5. Regenerate `openapi.json`.
6. Run focused validation, then create the validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_deps.py -q`
  passes.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check` passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` passes for the
  touched Python surface.
- `uv run --python 3.12 --extra dev mypy src/awf` passes.

## Assumptions/Changes

- The workspace's bare `python` interpreter does not have project dependencies
  installed, so OpenAPI generation and drift checks are run through `uv run`
  with the repository's dev extras.
