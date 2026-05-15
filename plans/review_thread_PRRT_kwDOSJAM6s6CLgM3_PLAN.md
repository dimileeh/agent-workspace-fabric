# Review Thread PRRT_kwDOSJAM6s6CLgM3 Plan

## Problem Statement and Scope

The protected callback endpoints expose an `Authorization` header in OpenAPI,
but their generated response contracts do not list the structured auth and
runtime error statuses that the routes can return. The scope is limited to
`GET /v1/callbacks`, `POST /v1/callbacks`, the generated `openapi.json`
artifact, and focused regression coverage.

## Requirements Checklist

- Document `401 Unauthorized` and `503 Service Unavailable` responses for both
  callback operations.
- Document callback registration error responses that the route currently
  returns: `400 Bad Request` for missing or invalid `Idempotency-Key`, and
  `409 Conflict` for idempotency conflicts.
- Reference the shared `ErrorResponse` schema for those structured error
  responses, following existing API route metadata patterns.
- Do not document `403 Forbidden` unless current code actually emits it.
- Keep `openapi.json` generated from the FastAPI app.

## Implementation Steps

1. Add a regression assertion in the OpenAPI artifact test for the callback
   error responses and schemas.
2. Update `src/awf/api/routes/callbacks.py` route metadata to declare the
   relevant error responses.
3. Regenerate `openapi.json` with `scripts/generate_openapi.py`.
4. Run focused OpenAPI validation and callback-related tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passes.

## Assumptions/Changes

- The workspace's plain `python` interpreter does not have project
  dependencies installed, so OpenAPI generation and checks run through
  `uv run --python 3.12 --extra dev`.
