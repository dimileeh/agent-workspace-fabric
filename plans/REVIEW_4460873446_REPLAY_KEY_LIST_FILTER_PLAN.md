# Review 4460873446 Replay Key List Filter Plan

## Problem Statement And Scope

The review-level comment for `issue:4460873446` reports that
`CallbackSubscriptionRepository.list_idempotency_replay_keys()` does not apply
the same non-NULL replay-key filters as the workspace helper. It also calls out
that the replay-key list helpers are not invoked from route handlers, making
their intended non-request-path use unclear.

Scope is limited to repository/service helper documentation, the callback
repository filter, and focused regression coverage. Route behavior should not
change: fresh over-limit requests must continue to use exact-key durable probes
instead of full-table replay-key warmups.

## Requirements Checklist

- Add a regression proving the callback replay-key list query filters out NULL
  idempotency keys and NULL request hashes.
- Add the missing callback repository filters.
- Document that replay-key list helpers are bounded support hooks and are not
  used by request handlers for admission bypass.
- Preserve the existing route tests that fail if fresh rejected requests call
  the list helpers.
- Run focused repository/API validation and diff hygiene checks.

## Implementation Steps

1. Add a failing callback repository regression around the emitted replay-key
   list query.
2. Update `CallbackSubscriptionRepository.list_idempotency_replay_keys()` to
   filter both `idempotency_key` and `request_hash`.
3. Add concise docstrings to the replay-key list helpers to make the
   non-request-path intent explicit.
4. Run the focused new regression before and after implementation, then rerun
   the nearby callback/workspace tests that protect request-path behavior.
5. Record validation evidence in
   `plans/REVIEW_4460873446_REPLAY_KEY_LIST_FILTER_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_replay_key_list_filters_null_legacy_rows -q`
  - Fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py::test_subscription_repository_lists_idempotency_replay_keys_with_limit tests/unit/db/test_workspace_repository.py::TestIdempotency::test_list_idempotency_replay_keys_is_bounded tests/unit/api/test_callbacks.py::test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_with_lock_scoped_replay_check -q`
  - Passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py src/awf/service/callbacks.py tests/unit/db/test_callback_repository.py`
  - Passes.
- `git diff --check`
  - Passes.
