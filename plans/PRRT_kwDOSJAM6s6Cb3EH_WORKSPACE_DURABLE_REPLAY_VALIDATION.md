# PRRT_kwDOSJAM6s6Cb3EH Workspace Durable Replay Validation

## Plan Conformance

- Preserved same-process replay behavior by leaving the existing pre-admission
  replay-key check and durable replay lookup in place.
- Preserved fresh over-quota rejection by only attempting durable replay after
  admission rejection when the idempotency key is known from the replay-key
  cache or from a one-time durable key warm.
- Fixed cache-loss durable replay for both `POST /v1/workspaces` and
  `POST /v2/workspaces`; the route now warms persisted workspace idempotency
  keys after a rate-limit rejection and reuses the normal durable replay
  response path.
- Preserved conflict behavior because concrete in-memory hashes still conflict
  immediately, and durable-warmed keys still flow through the database payload
  match check before returning a replay response.
- Avoided default positive-key eviction by making the workspace replay-key
  cache unbounded by default while preserving bounded behavior for explicit
  `max_entries` tests or future callers.

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_idempotency_replay_survives_cache_loss_when_rate_limited -q`
  - Before implementation: failed for both v1 and v2 with `429`.
  - After implementation: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_workspace_replay_key_cache_default_retains_keys_past_response_cache_limit -q`
  - Before implementation: failed because the first key was evicted.
  - After implementation: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_before_db_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q`
  - `4 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py src/awf/db/repositories.py tests/unit/api/test_workspaces.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/workspaces.py src/awf/db/repositories.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py -q`
  - `134 passed`.

## Remaining Gaps

No known gaps for this review thread.
