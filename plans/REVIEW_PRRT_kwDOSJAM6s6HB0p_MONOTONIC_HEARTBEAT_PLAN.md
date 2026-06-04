# Review PRRT_kwDOSJAM6s6HB0p Monotonic Heartbeat Plan

## Problem Statement And Scope

The review thread reports that PostgreSQL worker-heartbeat upserts can regress
`last_heartbeat_at` when overlapping writes for the same `worker_id` commit out
of timestamp order. Scope is limited to the worker heartbeat conflict update and
focused regression coverage for that behavior.

## Requirements Checklist

- Verify the current upsert can overwrite a newer heartbeat with an older one.
- Add focused regression coverage proving older conflicting writes do not
  regress `last_heartbeat_at`.
- Preserve metadata (`node_id`, `poll_interval_seconds`, `updated_at`) from the
  heartbeat row that owns the greatest `last_heartbeat_at`.
- Keep `started_at` unchanged across conflict updates.
- Avoid broad validation; AWF/GitHub owns full validation after agent
  completion.

## Implementation Steps

1. Update the heartbeat repository test to assert PostgreSQL conflict updates
   are monotonic and preserve metadata for the newest heartbeat.
2. Confirm the focused regression fails against the current unconditional
   conflict update.
3. Change `_worker_heartbeat_upsert_stmt` to select the greater existing or
   excluded heartbeat timestamp and conditionally update corresponding metadata.
4. Re-run the focused worker-heartbeat tests and narrow lint for touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py::test_worker_heartbeat_upsert_supports_postgres_only tests/unit/db/test_worker_heartbeats.py::test_record_heartbeat_preserves_newest_conflicting_write -q`
  - Passes after implementation; the new regression fails before implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py tests/unit/db/test_worker_heartbeats.py`
  - Passes with no lint findings.

Full AWF/GitHub validation is managed after agent completion and is not executed
inside this agent phase.
