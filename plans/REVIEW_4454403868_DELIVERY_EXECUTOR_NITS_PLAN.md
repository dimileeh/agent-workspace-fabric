# Review 4454403868 Delivery Executor Nits Plan

## Problem Statement And Scope

Address the current review-level follow-up for PR #249. The actionable scope is
limited to callback delivery timeout observability, callback DNS executor
lifecycle, and documentation of the conservative resolved-IP validation policy.

## Requirements Checklist

- Distinguish `asyncio.wait_for` delivery-budget timeouts from ordinary poster
  failures in `_post_to_validated_callback_addresses`.
- Ensure delivery-budget exhaustion during validated-address posting is recorded
  through `CALLBACK_DELIVERY_BUDGET_EXCEEDED`, not `CALLBACK_REQUEST_FAILED`.
- Lazily create the callback target validation executor on first use instead of
  constructing it during module import.
- Preserve explicit executor shutdown semantics for FastAPI lifespan and tests.
- Document the all-or-nothing resolved-IP public routability policy so future
  maintainers do not weaken the DNS-rebinding protection by mistake.
- Keep the existing security behavior for rejecting any hostname resolution set
  containing a non-public address.

## Implementation Steps

1. Add focused regression tests for import-time executor laziness and validated
   address POST `wait_for` timeout classification.
2. Update the callback service so the executor starts as `None` and is created
   only by `_callback_target_validation_executor()`.
3. Wrap poster exceptions separately from the `asyncio.wait_for` guard timeout,
   raising `CallbackDeliveryBudgetExceededError` for guard timeout or exhausted
   remaining delivery budget.
4. Add a concise comment above the all-address DNS public-IP check explaining
   why mixed public/non-public resolution is rejected instead of filtered.
5. Run narrow callback tests, then lint/typecheck for touched Python code.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_callbacks.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  must pass.
