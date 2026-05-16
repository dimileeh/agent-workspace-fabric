# OpenAPI Callback Auth Header Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CLejD` reports that `/v1/callbacks` OpenAPI
operations expose the `authorization` header with `required: false` even though
the endpoints require `Authorization: Bearer <token>` for successful requests.

Scope is limited to correcting generated OpenAPI metadata and the checked-in
`openapi.json` artifact while preserving existing runtime auth error behavior.

## Requirements Checklist

- Add or update a regression test proving both `/v1/callbacks` operations mark
  the `authorization` header as required.
- Preserve the existing `require_api_token` runtime behavior for missing config,
  invalid tokens, and valid tokens.
- Update generated `openapi.json` from the app so the checked-in artifact matches
  current code.
- Run narrow validation for the OpenAPI regression and drift check.

## Implementation Steps

1. Update the callback OpenAPI artifact test to assert `required: true`.
2. Confirm the updated test fails against the current implementation.
3. Post-process generated OpenAPI operations that expose the auth dependency's
   `authorization` header so the contract marks the header required.
4. Regenerate `openapi.json`.
5. Run focused tests and the OpenAPI drift check.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_callback_endpoints_expose_authorization_header_in_openapi -q`
  passes after implementation.
- `python scripts/generate_openapi.py --check` reports no drift.
