# PRRT_kwDOSJAM6s6CQHoH Callback POST Wall Timeout Plan

## Problem Statement and Scope

The callback delivery path tracks a delivery deadline and passes the remaining budget into the HTTP poster. The default HTTPX poster treats that float as per-phase timeout settings, so a pinned callback POST can exceed the subscription's wall-clock delivery budget while waiting for a response phase. Scope is limited to enforcing the existing remaining delivery budget around each validated-address POST attempt.

## Requirements Checklist

- Add a regression test showing each validated-address POST attempt is wrapped in the remaining wall-clock timeout.
- Preserve the existing validated-address fallback behavior and error aggregation.
- Keep the default HTTPX poster API unchanged while continuing to pass the remaining timeout into HTTPX.
- Run the narrow callback unit tests that cover the changed behavior.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_callbacks.py` for the wall-clock wrapper around `_post_to_validated_callback_addresses`.
2. Confirm the regression fails before implementation.
3. Wrap the `poster(...)` await in `src/awf/service/callbacks.py` with `asyncio.wait_for(..., timeout=remaining_timeout)`.
4. Re-run the focused callback tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passes with the new regression and existing callback tests green.
