# PRRT_kwDOSJAM6s6Cb3EE Callback Durable Replay Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Cb3EE` reports that callback registration
replays can return `429` when the in-memory response cache and positive
idempotency-key cache no longer know an already-persisted callback row. The
existing admission regression also requires fresh over-quota idempotency keys to
avoid per-key `CallbackService.replay_existing()` database misses.

Scope is limited to `POST /v1/callbacks` durable replay ordering, the callback
idempotency key cache, focused API/repository tests, and this plan/validation
pair.

## Requirements Checklist

- [ ] Preserve existing in-memory response replay behavior.
- [ ] Preserve fresh over-quota key rejection before `CallbackService.replay_existing()`.
- [ ] Let persisted callback replays bypass callback registration rate limiting
      after response/key cache loss.
- [ ] Avoid default eviction of positive replay keys for accepted callback
      registrations.
- [ ] Preserve idempotency conflict behavior for same key with changed payload.
- [ ] Add regression coverage for durable replay after both route caches are
      cold.
- [ ] Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Add a focused callback API regression that consumes the registration quota,
   clears both callback replay caches, then verifies the same persisted
   `Idempotency-Key` and payload still returns the original subscription.
2. Add a cache unit assertion proving the default positive replay-key cache does
   not evict accepted keys at the response-cache entry limit.
3. Extend the callback subscription repository/service with a narrow method that
   lists persisted `(idempotency_key, request_hash)` pairs.
4. Extend `_CallbackIdempotencyReplayKeyCache` so production defaults retain all
   accepted keys and can be warmed once from durable rows.
5. In `register_callback`, when admission rejects an unknown key, warm the
   positive key cache from durable rows and retry the positive-key replay path
   before returning `429`.
6. Run the focused failing-first test before implementation, then run the
   targeted callback test surface and lint/type checks after implementation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_db_replay_bypasses_limit_when_replay_caches_are_cold -q`
  - Fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - All callback API tests pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_callback_repository.py -q`
  - Callback repository tests pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/callbacks.py src/awf/service/callbacks.py src/awf/db/repositories.py tests/unit/api/test_callbacks.py tests/unit/db/test_callback_repository.py`
  - No lint findings.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/callbacks.py src/awf/service/callbacks.py src/awf/db/repositories.py`
  - No type errors.
