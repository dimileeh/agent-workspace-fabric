# PRRT_kwDOSJAM6s6CNZjW Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CNZjW_PLAN.md`

## Requirement Status

- Complete: Regression coverage now includes
  `GET /v1/workspaces/{workspace_id}/runtime` and
  `GET /v1/workspaces/{workspace_id}/validation` in the protected REST/OpenAPI
  contract via `tests/unit/api/test_openapi_artifact.py`,
  `tests/unit/contracts/test_surface_metadata_alignment.py`, and
  `tests/unit/contracts/_capabilities.py`.
- Complete: `src/awf/api/routes/runtime.py` and
  `src/awf/api/routes/validation.py` now attach router-level
  `Depends(require_api_token)` and `API_TOKEN_AUTH_ERROR_RESPONSES`.
- Complete: `openapi.json` was regenerated and now advertises `bearerAuth`,
  `401`, and `503` for both sibling metadata routes.
- Complete: Focused validation commands passed in the repo's `uv --extra dev`
  environment.

## Evidence

- Initial TDD failure before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated tests/unit/contracts/test_surface_metadata_alignment.py::test_workspace_metadata_routes_remain_auth_protected tests/unit/contracts/test_auth_failure_alignment.py::test_every_registry_protected_rest_route_rejects_wrong_bearer -k "workspace_runtime or workspace_validation" -q`
  failed because both routes returned `404 NOT_FOUND` instead of `401
  UNAUTHORIZED` for a wrong bearer token.
- Passing focused auth contract:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated tests/unit/contracts/test_surface_metadata_alignment.py::test_workspace_metadata_routes_remain_auth_protected tests/unit/contracts/test_auth_failure_alignment.py::test_every_registry_protected_rest_route_rejects_wrong_bearer -q`
  passed with `39 passed`.
- Passing lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/runtime.py src/awf/api/routes/validation.py tests/unit/api/test_openapi_artifact.py tests/unit/contracts/_capabilities.py tests/unit/contracts/test_surface_metadata_alignment.py`
  passed.
- Passing OpenAPI drift check:
  `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.

## Gaps

None.
