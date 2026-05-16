# PRRT_kwDOSJAM6s6CZRSn Callback Retry-After Plan

## Problem Statement and Scope

Callback registration rate-limit responses include `retry_after_seconds` in the
JSON error detail, but do not forward that value as a `Retry-After` response
header. The scope is limited to the callback registration 429 response and its
unit API contract.

## Requirements Checklist

- Add a regression assertion that callback registration 429 responses expose
  `Retry-After`.
- Populate the `Retry-After` header from
  `decision.metadata["retry_after_seconds"]`, matching workspace create
  admission behavior.
- Preserve the existing JSON error body and metadata.

## Implementation Steps

1. Update the callback rate-limit test helper to assert the response header.
2. Run the focused callback test to confirm the missing header fails.
3. Add the header to `_callback_register_rate_limited_response`.
4. Re-run the focused callback tests.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  must pass.
