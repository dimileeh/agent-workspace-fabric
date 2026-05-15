# Review 4460873446 Callback Key Cache Bound Plan

## Problem Statement And Scope

The PR review-level comment for `issue:4460873446` reports that callback
registration's idempotency replay-key cache is instantiated without a
`max_entries` bound. The all-key durable warmup described in the comment is
stale in this branch because the rejected-request path now performs a
single-key lookup, but the production app-state cache still keeps every
accepted callback idempotency key for the process lifetime.

Scope is limited to callback replay-key cache construction, focused regression
coverage, and this plan/validation record. The explicit
`_CallbackIdempotencyReplayKeyCache()` default remains unbounded so existing
durable-replay regression coverage and direct cache semantics are not weakened.

## Requirements Checklist

- Add a regression proving the callback replay-key cache created for app state
  evicts older keys at the configured callback replay cache limit.
- Preserve the existing explicit unbounded cache default test.
- Avoid changing callback registration replay, conflict, and rate-limit
  behavior outside cache capacity.
- Keep the change scoped to callback route code, callback API tests, and
  plan/validation docs.

## Implementation Steps

1. Add a failing unit test that obtains the callback replay-key cache through a
   real request with `app.state`, inserts one entry beyond the configured limit,
   and verifies the oldest key is evicted.
2. Update callback replay-key cache factory construction to pass the configured
   `max_entries` bound for production/request-local caches.
3. Run the focused new regression first to show the old implementation fails,
   then rerun the callback API test module.
4. Record verification evidence in
   `plans/REVIEW_4460873446_CALLBACK_KEY_CACHE_BOUND_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_callback_replay_key_cache_app_state_is_bounded -q`
  - Fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - All callback API tests pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py tests/unit/api/test_callbacks.py`
  - No lint findings.
