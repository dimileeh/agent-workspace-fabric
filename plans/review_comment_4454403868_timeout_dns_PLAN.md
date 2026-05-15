# Review Comment 4454403868 Timeout And DNS Plan

## Problem Statement and Scope

PR #249 received a review-level callback delivery comment with two actionable
observability/resilience gaps:

- `_post_to_validated_callback_addresses` can report a prior connection failure
  when the delivery budget is actually exhausted before the next validated IP
  address can be attempted.
- `_validate_callback_target_with_timeout` offloads blocking DNS validation via
  `asyncio.to_thread`, so timed-out `getaddrinfo` work can keep occupying slots
  in asyncio's shared default thread pool.

Scope is limited to callback delivery timeout signaling, callback DNS validation
offload isolation, focused unit tests, and this plan/validation record.

## Requirements Checklist

- Add a regression test proving timeout-budget exhaustion after an attempted
  address raises `TimeoutError`, not the prior connection exception.
- Preserve attempted-address failure details as causal traceback evidence when
  the final delivery signal is timeout.
- Preserve aggregate per-address failure reporting when all attempted validated
  addresses fail before the timeout budget is exhausted.
- Move callback target validation off asyncio's shared default thread pool and
  into a callback-specific bounded executor.
- Keep the existing delivery timeout around the whole validation call.
- Avoid unsafe process-global DNS timeout changes.
- Do not push, switch branches, or write any GitHub comment.

## Implementation Steps

1. Add/update focused service-unit tests for timeout-after-failure signaling and
   callback-specific executor usage.
2. Confirm the focused tests fail before implementation where practical.
3. Update `_post_to_validated_callback_addresses` to prioritize timeout-budget
   exhaustion while retaining previous failures as an `ExceptionGroup` cause.
4. Add a bounded callback validation executor and helper used by
   `_validate_callback_target_with_timeout` instead of `asyncio.to_thread`.
5. Run the focused tests, then the callback service unit module and lint/type
   checks for touched code.
6. Record results in
   `plans/review_comment_4454403868_timeout_dns_VALIDATION.md`.
7. Stage only changed files and commit locally with the requested review-comment
   message format.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'timeout_after_failure or fallback_stops_when_timeout_budget_is_exhausted or offloads_callback_target_validation or counts_callback_target_validation'`
  fails before implementation for the new/updated expectations and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
