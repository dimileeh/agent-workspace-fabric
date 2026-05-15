# Callback Delivery Budget Cap Plan

## Problem Statement and Scope

The callback delivery poster wrapper currently calls `asyncio.wait_for` with
`remaining_timeout + 1`. That leaves one callback POST attempt able to overrun
the remaining delivery budget by about one second when the poster ignores its
own timeout. Scope is limited to enforcing the wall-clock cap for validated
callback address POST attempts and updating focused regression coverage.

## Requirements Checklist

- Enforce the remaining callback delivery budget as the `asyncio.wait_for` cap.
- Preserve the existing remaining-budget value passed into the poster.
- Preserve existing timeout error classification as
  `CallbackDeliveryBudgetExceededError`.
- Keep changes scoped to callback delivery behavior and its tests.

## Implementation Steps

1. Update the focused regression test to expect `asyncio.wait_for` to receive
   exactly the remaining delivery timeout.
2. Run the focused test and confirm it fails before implementation.
3. Change `_post_to_validated_callback_addresses` to use the remaining timeout
   as the wrapper timeout.
4. Run the focused callback tests and relevant quality checks.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout -q`
  passes after failing before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
