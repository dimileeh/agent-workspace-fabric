# Review Thread PRRT_kwDOSJAM6s6CaS80 Plan

## Problem Statement And Scope

PR review reports that three newly documented 429 responses expose the
`ErrorResponse` body in OpenAPI but omit the `Retry-After` response header that
the runtime limiter already returns. Scope is limited to the OpenAPI metadata
for:

- `POST /v1/callbacks`
- `POST /v1/workspaces`
- `POST /v2/workspaces`

## Requirements Checklist

- Each affected 429 response must document a `Retry-After` header.
- The header description must tell clients it is for backoff and allow the
  standard `Retry-After` string forms.
- The 429 responses must continue to document the `ErrorResponse` body.
- The checked-in `openapi.json` artifact must match the generated app spec.
- A regression test must cover the three affected OpenAPI responses.

## Implementation Steps

1. Add a failing OpenAPI regression test for the three affected 429 responses.
2. Add shared `Retry-After` header metadata for rate-limited responses.
3. Apply the shared response metadata to callback registration and both
   workspace create routes.
4. Regenerate `openapi.json`.
5. Run the focused OpenAPI test and spec drift check.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  passes.
- `python scripts/generate_openapi.py --check` passes.
