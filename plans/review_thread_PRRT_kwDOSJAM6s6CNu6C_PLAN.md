# Review Thread PRRT_kwDOSJAM6s6CNu6C Plan

## Problem Statement and Scope

PR review reports that callback target validation performs DNS resolution before the
delivery timeout budget starts. A stalled resolver can block `drain_due` longer than
the configured per-delivery timeout and delay later due callbacks.

Scope is limited to `src/awf/service/callbacks.py`, focused callback service tests,
and this plan/validation record.

## Requirements Checklist

- Bound callback target validation, including DNS resolution, by the subscription
  delivery timeout.
- Reuse one per-delivery deadline across validation and the POST attempt so DNS
  time consumes POST budget.
- Preserve invalid-target handling for `ValueError` validation failures.
- Treat validation timeout as a delivery/request failure eligible for retry.
- Add focused regression coverage.

## Implementation Steps

1. Add a failing unit test that makes validation consume a fake event-loop clock
   budget and asserts the POST receives only the remaining timeout.
2. Add timeout/deadline handling around callback target validation in
   `CallbackDeliveryService.drain_due`.
3. Keep existing request failure and invalid target metadata behavior intact.
4. Run the narrow callback service test selection and lint/type checks if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
