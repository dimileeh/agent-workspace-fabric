# Global Events Auth Plan

## Problem Statement

The global `GET /v1/events` route exposes workspace event metadata across workspaces without `require_api_token`, while the workspace metadata hardening contract and parity docs require event metadata surfaces to be bearer authenticated.

## Requirements Checklist

- Add `require_api_token` protection to `GET /v1/events`.
- Document the global events route as a protected bearer-auth route in OpenAPI metadata.
- Extend regression coverage so `GET /v1/events` remains in the auth-protected metadata contract.
- Keep existing event-listing behavior unchanged for authenticated callers.

## Implementation Steps

1. Update contract and OpenAPI tests to include `GET /v1/events` in protected route sets.
2. Add `require_api_token` and auth error responses to the global events router.
3. Update event API tests to use bearer auth for protected requests.
4. Regenerate `openapi.json` and update REST reference text for the new auth requirement.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py tests/unit/api/test_events.py tests/unit/api/test_pagination_envelopes.py tests/unit/api/test_openapi_artifact.py -q`
- `python scripts/generate_openapi.py --check`
