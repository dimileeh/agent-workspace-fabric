# PRRT_kwDOSJAM6s6COw7_ Auth Response Metadata Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6COw7__PLAN.md`

## Requirement Status

- Complete: Extended the OpenAPI regression test so the reported protected
  task, lock, merge queue, release readiness, and metrics routes must document
  bearer auth plus 401 and 503 auth failure responses.
- Complete: Confirmed the extended regression failed before implementation.
  Evidence: the focused test failed with `GET /release-readiness must document
  401`.
- Complete: Added shared `API_TOKEN_AUTH_ERROR_RESPONSES` metadata to the
  affected routers/routes without changing runtime auth dependencies.
- Complete: Regenerated `openapi.json`.
- Complete: Ran focused OpenAPI, drift, contract, and lint validation.

## Files Changed

- `src/awf/api/routes/tasks.py`
- `src/awf/api/routes/locks.py`
- `src/awf/api/routes/merge_queue.py`
- `src/awf/api/routes/metrics.py`
- `src/awf/api/routes/health.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`
- `plans/PRRT_kwDOSJAM6s6COw7__PLAN.md`
- `plans/PRRT_kwDOSJAM6s6COw7__VALIDATION.md`

## Commands Run

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated -q`
  failed with `GET /release-readiness must document 401`.
- Green focused check: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated -q`
  passed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
  regenerated the checked-in artifact.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_docs_drift.py tests/unit/contracts/test_surface_metadata_alignment.py -q`
  passed: `149 passed`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes tests/unit/api/test_openapi_artifact.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes`
  passed.
