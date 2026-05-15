# Callback Auth CI Plan

## Problem Statement And Scope

PR #249 fails CI in `tests/unit/api/test_callbacks.py` because the callback
authorization regression tests expect missing bearer tokens to return `401`, but
the shared `client` fixture includes a default `Authorization` header. The
callback API implementation already declares `Depends(require_api_token)` on the
register and list routes; the failing tests are not exercising the intended
missing-token path.

Scope is limited to the callback API tests and required plan/validation
artifacts. No production auth behavior should be weakened.

## Requirements Checklist

- Confirm the reported failing tests fail locally before editing.
- Preserve the callback routes' `require_api_token` protection.
- Make missing-token callback tests send requests without a default
  `Authorization` header.
- Keep invalid-token callback tests intact so both missing and invalid auth
  failures remain covered.
- Run focused verification for the callback auth tests and a reasonable
  callback API unit surface after the fix.

## Implementation Steps

1. Use the existing failing assertions as the TDD red test.
2. Update the two missing-token tests to use `callback_app_and_client`, whose
   `AsyncClient` does not pre-populate authorization headers.
3. Re-run the two focused failing tests.
4. Re-run `tests/unit/api/test_callbacks.py` to guard nearby callback behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_requires_authorization_token tests/unit/api/test_callbacks.py::test_list_callbacks_requires_authorization_token -q`
  - Passes with both tests returning `401` and `UNAUTHORIZED`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Passes all callback API unit tests.
