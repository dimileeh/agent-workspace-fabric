# Review 4454403868 Delivery Executor Nits Validation

Plan reference: `plans/REVIEW_4454403868_DELIVERY_EXECUTOR_NITS_PLAN.md`

## Requirement Status

- Complete: Distinguish `asyncio.wait_for` delivery-budget timeouts from
  ordinary poster failures in `_post_to_validated_callback_addresses`.
  - Evidence: Poster exceptions are wrapped and unwrapped separately from the
    outer `wait_for` timeout guard.
- Complete: Ensure delivery-budget exhaustion during validated-address posting
  is recorded through `CALLBACK_DELIVERY_BUDGET_EXCEEDED`.
  - Evidence: Validated-address posting now raises
    `CallbackDeliveryBudgetExceededError` when the guard timeout fires or when
    the remaining budget is exhausted before another address can be attempted.
- Complete: Lazily create the callback target validation executor on first use.
  - Evidence: `_CALLBACK_TARGET_VALIDATION_EXECUTOR` starts as `None`; the
    accessor still creates it under the existing lock.
- Complete: Preserve explicit executor shutdown semantics.
  - Evidence: Existing shutdown test still verifies `shutdown(wait=False,
    cancel_futures=True)` and reset-to-`None` behavior.
- Complete: Document the all-or-nothing resolved-IP public routability policy.
  - Evidence: `_validate_callback_target_dns` now explains why mixed
    public/non-public DNS answers are rejected instead of filtered.
- Complete: Keep the existing security behavior for rejecting any hostname
  resolution set containing a non-public address.
  - Evidence: Existing private IP, NAT64, and 6to4 callback target rejection
    tests continue to pass.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/REVIEW_4454403868_DELIVERY_EXECUTOR_NITS_PLAN.md`
- `plans/REVIEW_4454403868_DELIVERY_EXECUTOR_NITS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'validated_address_post_attempt_uses_remaining_wall_clock_timeout or callback_target_validation_executor_is_lazy_at_import or validated_address_timeout_after_failure_raises_timeout_with_prior_failure_cause or validated_address_fallback_stops_when_timeout_budget_is_exhausted'`
  - Failed before implementation with four expected failures.
  - Passed after implementation: `4 passed, 42 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: `46 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
