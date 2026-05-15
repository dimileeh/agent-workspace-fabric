# Operator Metadata Auth Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6COZTx` reports that the MCP parity contract
documents non-health/readiness operator routes as API-token protected, but some
REST metadata routers are still registered without `require_api_token`.

Scope is limited to protecting the documented operator metadata routes and
adding/adjusting regression coverage for the missing auth dependency.

## Requirements Checklist

- Confirm current tests fail for at least one unprotected documented operator
  metadata route.
- Add `require_api_token` to tasks, merge queue, locks, metrics, and
  `/release-readiness` route metadata as needed.
- Keep `/healthz` and `/readyz` explicitly public.
- Preserve existing response schemas and route behavior for authenticated
  callers.
- Validate with the narrow contract/API tests covering auth metadata and
  unauthorized requests.

## Implementation Steps

1. Run the narrow surface metadata contract test before code changes.
2. Add or expand regression coverage so the specific operator metadata routes
   in the review are asserted auth-protected.
3. Update the affected routers to include `Depends(require_api_token)`.
4. Run the focused contract/API tests and lint relevant files if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py -q`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_auth_failure_alignment.py -q`
  should pass for REST auth failure behavior if runtime permits.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes tests/unit/contracts/test_surface_metadata_alignment.py`
  must pass.
