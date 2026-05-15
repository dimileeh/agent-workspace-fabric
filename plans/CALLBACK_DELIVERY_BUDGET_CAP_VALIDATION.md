# Callback Delivery Budget Cap Validation

Plan reference: `CALLBACK_DELIVERY_BUDGET_CAP_PLAN.md`

## Requirement Status

- Enforce the remaining callback delivery budget as the `asyncio.wait_for` cap:
  Complete. `_post_to_validated_callback_addresses` now passes
  `remaining_timeout` directly to `asyncio.wait_for`.
- Preserve the existing remaining-budget value passed into the poster:
  Complete. The poster still receives `timeout=remaining_timeout`.
- Preserve existing timeout error classification as
  `CallbackDeliveryBudgetExceededError`: Complete. The focused regression
  still asserts the raised error type and cause.
- Keep changes scoped to callback delivery behavior and its tests: Complete.
  Production changes are limited to `src/awf/service/callbacks.py`, with the
  focused regression in `tests/unit/service/test_callbacks.py`.

## Evidence

- Pre-implementation regression run:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout -q`
  failed with `wait_for_timeouts == [11.0]` instead of `[10.0]`.
- Post-implementation focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_post_attempt_uses_remaining_wall_clock_timeout -q`
  passed.
- Callback service test module:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passed with 50 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passed.

## Remaining Gaps

None.
