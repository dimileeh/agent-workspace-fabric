# Callback Poster ValueError Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CNdgV` reports that `CallbackDeliveryService.drain_due`
classifies any `ValueError` raised while posting a validated callback as
`CALLBACK_TARGET_INVALID`. Target policy rejection should only cover
`_validate_callback_target`; poster failures should use the existing request-failure path.

Scope is limited to callback delivery failure classification and a focused regression test.

## Requirements Checklist

- Add a regression test proving a poster-raised `ValueError` is recorded as
  `CALLBACK_REQUEST_FAILED`, not `CALLBACK_TARGET_INVALID`.
- Keep validation-raised `ValueError` behavior unchanged.
- Split `drain_due` exception handling so `ValueError` is caught only around target validation.
- Run the narrow callback service test that proves the fix.

## Implementation Steps

1. Add the regression test to `tests/unit/service/test_callbacks.py`.
2. Run the new test and confirm it fails against the current implementation.
3. Split the validation and posting `try` blocks in `src/awf/service/callbacks.py`.
4. Re-run the focused callback service tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`

Pass criteria: the callback service test module passes, including the new regression.
