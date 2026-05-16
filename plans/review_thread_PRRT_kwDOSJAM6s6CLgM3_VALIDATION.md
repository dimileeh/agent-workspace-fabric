# Review Thread PRRT_kwDOSJAM6s6CLgM3 Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CLgM3_PLAN.md`

## Requirement Status

- Complete: Document `401 Unauthorized` and `503 Service Unavailable` for both
  callback operations.
- Complete: Document `400 Bad Request` and `409 Conflict` for callback
  registration errors.
- Complete: Error responses reference `#/components/schemas/ErrorResponse` in
  generated OpenAPI.
- Complete: `403 Forbidden` is not documented because the current callback
  route code does not emit it.
- Complete: `openapi.json` was regenerated from the FastAPI app.

## Evidence

Files changed:

- `src/awf/api/routes/callbacks.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`
- `plans/review_thread_PRRT_kwDOSJAM6s6CLgM3_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6CLgM3_VALIDATION.md`

Commands run:

- Expected failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_document_structured_error_responses -q`
  failed because `GET /v1/callbacks` only exposed `200` and `422`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passed: 10 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  passed: 53 passed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_openapi_artifact.py`
  passed.

Note: plain `python scripts/generate_openapi.py` failed in this workspace
because the plain interpreter lacked `fastapi`; the same script passed through
the repo's `uv` environment.
