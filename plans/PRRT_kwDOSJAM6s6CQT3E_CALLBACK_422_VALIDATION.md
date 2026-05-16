# Validation

Plan reference: `PRRT_kwDOSJAM6s6CQT3E_CALLBACK_422_PLAN.md`

# Requirement Status

- `POST /v1/callbacks` 422 OpenAPI response must not advertise only `HTTPExceptionErrorResponse`: Complete.
- `POST /v1/callbacks` 422 OpenAPI response must include the default `HTTPValidationError` schema: Complete.
- Callback target policy violations must remain documented as a structured `HTTPExceptionErrorResponse` 422: Complete.
- Runtime validation errors must keep FastAPI's standard validation-error shape: Complete.
- Checked-in `openapi.json` must match generated app output: Complete.

# Evidence

Files changed:

- `src/awf/api/routes/callbacks.py`
- `tests/unit/api/test_callbacks.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_document_structured_error_responses tests/unit/api/test_callbacks.py::test_register_callback_validation_errors_keep_fastapi_shape -q`
  - First run before implementation: failed on the OpenAPI 422 description/schema expectation, proving the regression test.
  - Final run after implementation: passed, `2 passed`.
- `python scripts/generate_openapi.py --check`
  - Failed because the bare Python interpreter in this workspace lacks FastAPI.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - First run after implementation: reported `openapi.json` drift.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
  - Regenerated `openapi.json`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Final run: passed, `openapi.json matches the current app spec`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py tests/unit/api/test_openapi_artifact.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/api/test_openapi_artifact.py -q`
  - Passed, `73 passed`.

# Gaps

None.
