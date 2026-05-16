# PRRT_kwDOSJAM6s6CNZjW Plan

## Problem Statement and Scope

The PR review thread reports that `GET /v1/workspaces/{workspace_id}/runtime`
and `GET /v1/workspaces/{workspace_id}/validation` are included as separate
routers, so the workspace router-level API token dependency does not protect
them. The scope is limited to making those sibling workspace metadata endpoints
enforce and advertise bearer-token authentication consistently with the REST/MCP
contract.

## Requirements Checklist

- Add regression coverage proving the runtime and validation metadata routes are
  part of the auth-protected REST/OpenAPI contract.
- Add `require_api_token` router dependencies to the runtime and validation
  routers without changing endpoint behavior beyond auth failure handling.
- Regenerate `openapi.json` so checked-in API documentation matches the app.
- Run focused validation commands that prove the review thread is addressed.

## Implementation Steps

1. Update contract/OpenAPI tests and capability metadata to include both sibling
   routes in the protected surface.
2. Run the focused tests before implementation to confirm they fail against the
   current router wiring.
3. Add router-level API token dependencies and auth error response metadata to
   `src/awf/api/routes/runtime.py` and `src/awf/api/routes/validation.py`.
4. Regenerate `openapi.json`.
5. Run focused tests and the OpenAPI drift check.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_api_token_routes_are_documented_as_bearer_authenticated tests/unit/contracts/test_surface_metadata_alignment.py::test_workspace_metadata_routes_remain_auth_protected tests/unit/contracts/test_auth_failure_alignment.py::test_every_registry_protected_rest_route_rejects_wrong_bearer -k "workspace_runtime or workspace_validation" -q`
  - Passes after implementation and demonstrates both endpoints reject invalid
    bearer tokens and advertise bearer auth.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passes after regenerating `openapi.json`.

## Assumptions/Changes

- The workspace's bare `python` environment does not have FastAPI installed, so
  OpenAPI generation/checks are run through the repo's `uv --extra dev`
  environment.
