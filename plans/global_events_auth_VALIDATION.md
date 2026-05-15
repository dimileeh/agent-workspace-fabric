# Global Events Auth Validation

Plan reference: `plans/global_events_auth_PLAN.md`

## Requirement Status

- Add `require_api_token` protection to `GET /v1/events`: Complete. `src/awf/api/routes/events.py` now applies the bearer-token dependency to the global events router.
- Document the global events route as a protected bearer-auth route in OpenAPI metadata: Complete. `openapi.json` includes `bearerAuth`, `401`, and `503` metadata for `GET /v1/events`; `docs/REST_API_REFERENCE.md` shows the required header.
- Extend regression coverage so `GET /v1/events` remains in the auth-protected metadata contract: Complete. Contract and OpenAPI tests include the global events route in protected route sets.
- Keep existing event-listing behavior unchanged for authenticated callers: Complete. Event and pagination tests now send auth headers and continue to assert the same response envelopes and filtering behavior.

## Evidence

- Confirmed red failure before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py::test_workspace_metadata_routes_remain_auth_protected -q`
- Passing verification:
  - `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py tests/unit/api/test_events.py tests/unit/api/test_pagination_envelopes.py tests/unit/api/test_openapi_artifact.py -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - `uv run --python 3.12 --extra dev mypy src/awf`
  - `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`

## Gaps

None.
