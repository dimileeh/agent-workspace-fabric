# Review Comment 4454403868 Timeout Follow-Up Plan

## Problem Statement and Scope

The review-level comment reports two callback-security follow-ups. The
locally-assigned NAT64 prefix gap is already addressed in the current checkout
by explicit `64:ff9b:1::/48` unmasking and focused tests. The remaining
actionable gap is in callback delivery: when the delivery timeout budget is
exhausted before any validated IP address can be attempted, the helper raises a
misleading `RuntimeError` about missing connect IP addresses.

Scope is limited to timeout semantics in
`src/awf/service/callbacks.py`, focused regression coverage in
`tests/unit/service/test_callbacks.py`, and plan/validation records.

## Requirements Checklist

- Preserve the existing local-use NAT64 callback-target behavior and tests.
- Add a regression test for non-empty validated IP addresses skipped because
  the timeout budget is already exhausted.
- Raise `TimeoutError` for timeout-budget exhaustion before the first address
  attempt instead of claiming there are no validated IP addresses.
- Preserve the existing `RuntimeError` for truly empty validated address lists.
- Keep existing fallback behavior and exception aggregation unchanged.
- Do not push, switch branches, or write any GitHub comment.

## Implementation Steps

1. Add a failing service-unit regression test for timeout exhaustion before any
   validated address attempt.
2. Confirm that focused regression fails before implementation.
3. Update `_post_to_validated_callback_addresses` to distinguish timeout
   exhaustion from an empty validated-address tuple.
4. Run focused callback service tests and callback target tests.
5. Record validation evidence in
   `plans/review_comment_4454403868_timeout_followup_VALIDATION.md`.
6. Stage only changed files and commit locally with a review-comment fix
   message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_validated_address_delivery_timeout_before_first_attempt_raises_timeout -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'validated_address_fallback or delivery_timeout_before_first_attempt'`
  passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  passes to confirm the already-present NAT64 local-use coverage remains green.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py tests/unit/common/test_callback_targets.py`
  passes.
