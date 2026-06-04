# Review 4620209013 Non-PostgreSQL Heartbeat Fallback Plan

## Problem Statement and Scope

Review-level comment `issue:4620209013` notes that
`WorkerHeartbeatRepository.record_heartbeat()` still uses a non-atomic
read-then-insert fallback outside PostgreSQL. The PostgreSQL path is already a
monotonic upsert; this plan addresses only the fallback branch so test and
unsupported dialect paths do not race on concurrent first writes.

## Requirements Checklist

- Add a focused regression that deterministically exposes the fallback
  concurrent first-write race.
- Replace the fallback read-then-insert path with a constraint-first,
  race-tolerant path.
- Preserve monotonic heartbeat semantics: older heartbeats must not overwrite a
  newer `last_heartbeat_at`, `node_id`, `poll_interval_seconds`, or
  `updated_at`.
- Keep the PostgreSQL upsert path unchanged.
- Run targeted tests and lint only for the changed repository behavior.

## Implementation Steps

1. Add a failing fallback concurrency regression to
   `tests/unit/db/test_worker_heartbeats.py`.
2. Confirm the new regression fails against the current read-then-insert
   fallback.
3. Update `WorkerHeartbeatRepository.record_heartbeat()` fallback logic to
   perform a monotonic update first, then try an insert, and recover from
   concurrent duplicate inserts by retrying the monotonic update.
4. Run the focused heartbeat repository tests and a targeted ruff check.
5. Create
   `plans/REVIEW_4620209013_NONPG_HEARTBEAT_FALLBACK_VALIDATION.md` with
   requirement-by-requirement evidence.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py::test_fallback_record_heartbeat_handles_concurrent_first_writes -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q -k "worker_heartbeat or record_heartbeat or prune_stale"`
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/system_repo.py tests/unit/db/test_worker_heartbeats.py`

Full AWF/GitHub validation remains owned by AWF after agent completion.
