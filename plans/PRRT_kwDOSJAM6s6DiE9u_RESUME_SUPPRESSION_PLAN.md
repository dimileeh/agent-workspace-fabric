# PRRT_kwDOSJAM6s6DiE9u Resume Suppression Plan

## Problem Statement And Scope

The requested-capacity scheduler can persist a resume cursor after scanning a
page that produced no claims. It currently reuses that cursor when allocation
and requested-queue signatures match. Provider recovery cooldowns and provider
model circuit breakers can expire with no queue or allocation mutation, so a
previously suppressed high-priority requested workspace may be skipped by later
polls.

Scope is limited to requested-capacity resume cursor invalidation for
time-based provider suppression. Existing ready execution pagination and
capacity blocking behavior should remain unchanged.

## Requirements Checklist

- Add a regression test that fails when a requested workspace suppressed by
  `PROVIDER_RECOVERY_NOT_BEFORE` is not revisited after its cooldown elapses.
- Reset the requested-capacity resume cursor once any observed provider
  suppression window that contributed to a skipped page has elapsed.
- Include provider model circuit breaker cooldowns in the same invalidation
  path when their `cooldown_until` is known.
- Preserve existing cursor reuse for purely capacity-blocked requested work.
- Keep changes scoped to worker scheduling code and focused tests.

## Implementation Steps

1. Add the failing requested-capacity regression test.
2. Track the earliest future provider suppression expiry while filtering
   scheduler candidate pages.
3. Store that expiry with the requested-capacity resume cursor and reject the
   cursor once the expiry time has passed.
4. Keep the existing simple candidate-filter API by wrapping a richer internal
   result.
5. Run the narrow regression tests, then worker unit tests as practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_resets_resume_cursor_when_provider_suppression_elapses or requested_capacity_gate_resumes_after_bounded_blocked_scan"`
  - Passes and demonstrates the new regression plus existing capacity-only
    resume behavior.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passes for the touched worker surface, time permitting.
