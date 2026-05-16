# Review 4460873446 Idempotency Plan

## Problem Statement and Scope

PR review comment `issue:4460873446` reports three idempotency admission concerns:

- `POST /v1/workspaces` returns a cache-hash conflict without the durable replay fallback already used by `POST /v2/workspaces`.
- Callback cold replay probing uses a hash lookup in one DB session, then fetches the row under an advisory lock in a second DB session.
- Shared in-memory LRU idempotency caches perform multi-step OrderedDict operations without a lock.

Scope is limited to the workspace and callback route admission paths and their focused regression tests.

## Requirements Checklist

- Add a v1 workspace regression proving a cache hash mismatch still replays from the durable row when the persisted payload matches.
- Change v1 workspace create to use the same durable fallback on cache conflict as v2 before returning `IDEMPOTENCY_CONFLICT`.
- Add/update callback regression coverage so cold durable replay locks and fetches in one path, without the pre-lock request-hash probe.
- Change callback persisted-key replay to use the lock-then-fetch durable replay path directly.
- Serialize composite LRU cache operations for callback replay caches and workspace replay-key cache.
- Preserve idempotency replay bypass before rate limiting and existing structured error responses.

## Implementation Steps

1. Add failing tests for the v1 durable fallback and callback lock-before-lookup behavior.
2. Update `src/awf/api/routes/workspaces.py` to fall back to durable replay in the v1 cache-conflict branch.
3. Update `src/awf/api/routes/callbacks.py` to remove the separate durable hash probe from request-path replay and lock cache mutations/lookups.
4. Add locking to `_WorkspaceCreateIdempotencyReplayKeyCache`.
5. Run narrow tests for the touched API routes, then lint/typecheck the touched surface as practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py src/awf/service/callbacks.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py`
- Pass criteria: tests and lint complete successfully, and the validation document records any remaining gap.
