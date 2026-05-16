# PRRT_kwDOSJAM6s6Cb3EH Workspace Durable Replay Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Cb3EH` reports that `POST /v1/workspaces`
and `POST /v2/workspaces` can return `429` for an already-persisted
idempotent workspace replay when the process-local positive replay-key cache is
cold or has evicted the key. The durable replay lookup currently runs only
after request admission unless the replay key cache already recognizes the key.

Scope is limited to workspace create replay-key cache behavior, workspace
repository key listing, the v1/v2 create route ordering around rate-limit
rejections, focused tests, and this plan/validation pair.

## Requirements Checklist

- [ ] Preserve existing same-process replay behavior.
- [ ] Preserve fresh over-quota idempotency-key rejection before per-key durable
      replay misses.
- [ ] Let persisted v1 and v2 workspace replays bypass workspace create rate
      limiting after replay-key cache loss.
- [ ] Preserve idempotency conflict behavior for known keys replayed with a
      different payload or API version.
- [ ] Avoid default eviction of positive workspace replay keys.
- [ ] Add/update focused regression coverage for cache-loss and replay-key
      retention behavior.
- [ ] Commit the fix locally without pushing or changing branches.

## Implementation Steps

1. Use the existing failing workspace API regression as the TDD signal for
   cache-loss durable replay under rate limiting.
2. Extend the workspace replay-key cache so the production default can retain
   positive keys past the old response-cache size, and can warm persisted keys
   from durable storage without needing payload hashes.
3. Add a narrow repository method that lists persisted workspace idempotency
   keys in stable order.
4. When v1/v2 create admission rejects an idempotent request, warm the positive
   key cache once from durable workspace rows, then retry only the known-key
   durable replay path before returning `429`.
5. Add a small cache regression for default retention past the old 4096-entry
   bound.
6. Run the focused failing test and the adjacent workspace route tests needed
   to prove fresh-key rate limiting and durable replay both hold.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_idempotency_replay_survives_cache_loss_when_rate_limited -q`
  - Fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limit_rejects_fresh_idempotency_key_before_db_replay_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_v1_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded tests/unit/api/test_workspaces.py::TestCreateWorkspaceV2DiskPressure::test_v2_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded -q`
  - Fresh over-quota keys remain rate-limited and same-key replays still pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/workspaces.py src/awf/db/repositories.py tests/unit/api/test_workspaces.py`
  - No lint findings in touched files.
