# OpenAPI Callback Auth Header Validation

Plan reference: `plans/OPENAPI_CALLBACK_AUTH_HEADER_PLAN.md`

## Requirement Status

- Add or update a regression test proving both `/v1/callbacks` operations mark
  the `authorization` header as required: Complete.
  Evidence: `tests/unit/api/test_openapi_artifact.py` now asserts
  `required: true` for the callback auth header and all generated authorization
  headers; the callback assertion failed before implementation.
- Preserve existing `require_api_token` runtime behavior: Complete.
  Evidence: implementation only post-processes OpenAPI metadata; the dependency
  signature and validation logic in `src/awf/api/deps.py` were not changed.
- Update generated `openapi.json`: Complete.
  Evidence: `openapi.json` regenerated through the project environment; protected
  operations now mark `authorization` as required.
- Run narrow validation for the OpenAPI regression and drift check: Complete.
  Evidence: commands below passed.

## Verification Evidence

- Red test before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_expose_authorization_header_in_openapi -q`
  failed with `GET /v1/callbacks Authorization header must be required`.
- Focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_expose_authorization_header_in_openapi -q`
  passed.
- Artifact drift:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- Static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
  `uv run --python 3.12 --extra dev mypy src/awf` passed.
- Related API tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_docs_drift.py tests/unit/api/test_deps.py -q`
  passed with 20 tests.

## Gaps

None.
