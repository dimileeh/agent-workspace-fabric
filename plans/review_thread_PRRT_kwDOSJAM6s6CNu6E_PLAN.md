# Review Thread PRRT_kwDOSJAM6s6CNu6E Plan

## Problem Statement and Scope

`GET /v1/callbacks` and `POST /v1/callbacks` currently document callback
HTTP error responses as top-level `ErrorResponse` objects, but the runtime
raises `HTTPException(detail={...})`. FastAPI serializes those responses as
`{"detail": {"error_code": ..., "message": ...}}`, which generated clients
will not deserialize correctly from the current OpenAPI artifact.

Scope is limited to the callback endpoint error-response documentation,
the checked-in OpenAPI artifact, and focused regression coverage.

## Requirements Checklist

- Preserve existing runtime behavior for callback `HTTPException` responses.
- Document callback `400`, `401`, `409`, and `503` responses with the actual
  FastAPI `detail` wrapper around `ErrorResponse`.
- Keep `422` validation responses on FastAPI's existing validation schema.
- Regenerate `openapi.json` from the current FastAPI app.
- Validate the focused OpenAPI and callback API surfaces.

## Implementation Steps

1. Update the OpenAPI artifact regression test to expect the callback error
   response schema wrapper.
2. Confirm the focused regression fails against the current implementation.
3. Add a reusable schema for structured `HTTPException` error responses.
4. Update `src/awf/api/routes/callbacks.py` response metadata to use that
   wrapper schema.
5. Regenerate `openapi.json`.
6. Run focused tests and spec drift checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_document_structured_error_responses -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/api/schemas.py tests/unit/api/test_openapi_artifact.py`
  passes.
