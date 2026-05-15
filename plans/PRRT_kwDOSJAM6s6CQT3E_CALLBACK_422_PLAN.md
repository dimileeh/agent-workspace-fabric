# Problem Statement

PR review thread `PRRT_kwDOSJAM6s6CQT3E` reports that `POST /v1/callbacks` documents every 422 response as `HTTPExceptionErrorResponse`, even though FastAPI/Pydantic validation failures still return the default `HTTPValidationError` shape.

# Scope

- Preserve existing runtime behavior for callback request validation and callback target policy failures.
- Update OpenAPI documentation so generated clients can distinguish validation 422s from structured callback policy 422s.
- Keep the fix limited to callback route schema/tests and generated OpenAPI artifacts if needed.

# Requirements Checklist

- `POST /v1/callbacks` 422 OpenAPI response must not advertise only `HTTPExceptionErrorResponse`.
- `POST /v1/callbacks` 422 OpenAPI response must include the default `HTTPValidationError` schema.
- Callback target policy violations must remain documented as a structured `HTTPExceptionErrorResponse` 422.
- Runtime validation errors must keep FastAPI's standard validation-error shape.
- Checked-in `openapi.json` must match generated app output.

# Implementation Steps

1. Add/update failing regression tests around the callback OpenAPI 422 response and validation shape.
2. Change the callback route 422 response declaration to document both response envelopes.
3. Regenerate `openapi.json` if the app schema changes.
4. Run focused tests and the OpenAPI drift check.

# Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_document_structured_error_responses tests/unit/api/test_callbacks.py::test_register_callback_validation_errors_keep_fastapi_shape -q`
- `python scripts/generate_openapi.py --check`
