# PRRT_kwDOSJAM6s6Ce2hx Plan

## Problem Statement And Scope

The callback registration endpoint attempts durable idempotency replay after a
429 admission rejection. The current rejected-request path warms the replay-key
cache by listing every callback idempotency key and request hash, so the first
fresh over-limit callback after process start can force an unbounded database
scan before returning 429.

Scope is limited to the callback registration durable replay lookup used after
rate-limit rejection. Existing replay semantics must stay intact: known
idempotency replays can still bypass the limiter, conflicting payloads still
return 409, and fresh over-limit keys still return 429 without registering.

## Requirements Checklist

- Add a regression test proving fresh over-limit callbacks do not call the
  all-key durable replay warmup.
- Replace all-key warmup on the rejected callback path with a bounded lookup for
  only the submitted idempotency key.
- Preserve existing idempotent replay, conflict, and rate-limit behavior.
- Keep changes scoped to callback route/service/repository support and tests.

## Implementation Steps

1. Add a failing API regression that makes `CallbackService.list_idempotency_replay_keys`
   raise during an over-limit fresh-key request.
2. Add a repository/service helper to fetch the stored request hash for one
   callback idempotency key.
3. Update `_callback_durable_replay_after_rejection` to use that single-key
   helper, remember the key hash when present, and avoid warming all durable
   replay keys.
4. Add focused repository/service coverage for the single-key hash helper if
   needed by the implementation surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py tests/unit/service/test_callbacks.py -q`
- Pass criteria: all commands pass, and the new regression fails before the
  implementation because the rejected path calls the all-key warmup.
