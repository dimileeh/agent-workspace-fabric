# Review 4460873446 Idempotency Validation

Plan reference: `REVIEW_4460873446_IDEMPOTENCY_PLAN.md`

## Requirement Status

- Complete: Added a v1 workspace regression proving a stale cache hash falls back to durable replay when the persisted payload matches.
- Complete: `POST /v1/workspaces` now mirrors v2 by attempting durable replay before returning `IDEMPOTENCY_CONFLICT` on replay-key cache hash conflict.
- Complete: Updated callback regression coverage so cold durable replay rejects the old pre-lock request-hash probe and verifies advisory lock before durable lookup.
- Complete: Callback persisted-key replay now uses a single lock-then-fetch durable replay path and populates the replay caches from the fetched row.
- Complete: Composite LRU cache operations are serialized for callback replay caches and the workspace replay-key cache.
- Complete: Idempotency replay bypass and structured conflict/rate-limit responses are preserved by the full workspace/callback API unit test files.

## Evidence

Files changed:

- `src/awf/api/routes/workspaces.py`
- `src/awf/api/routes/callbacks.py`
- `src/awf/service/callbacks.py`
- `tests/unit/api/test_workspaces.py`
- `tests/unit/api/test_callbacks.py`
- `plans/REVIEW_4460873446_IDEMPOTENCY_PLAN.md`
- `plans/REVIEW_4460873446_IDEMPOTENCY_VALIDATION.md`

Failing tests confirmed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_workspace_replay_key_cache_locks_composite_lru_operations tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_cache_hash_conflict_uses_durable_replay_before_conflict tests/unit/api/test_callbacks.py::test_register_callback_cold_replay_locks_before_durable_lookup tests/unit/api/test_callbacks.py::test_callback_replay_caches_lock_composite_lru_operations -q`
- Result before implementation: 4 failed for missing cache locks, v1 immediate 409 on cache hash conflict, and callback pre-lock hash probe.

Passing verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_workspace_replay_key_cache_locks_composite_lru_operations tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_cache_hash_conflict_uses_durable_replay_before_conflict tests/unit/api/test_callbacks.py::test_register_callback_cold_replay_locks_before_durable_lookup tests/unit/api/test_callbacks.py::test_callback_replay_caches_lock_composite_lru_operations -q`
- Result: 4 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py -q`
- Result: 228 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py src/awf/service/callbacks.py tests/unit/api/test_workspaces.py tests/unit/api/test_callbacks.py`
- Result: All checks passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/workspaces.py src/awf/api/routes/callbacks.py src/awf/service/callbacks.py`
- Result: Success, no issues found in 3 source files.

## Gaps

None.
