# Address Thread PRRT_kwDOSJAM6s6HAKY6 Plan

## Problem Statement and Scope

The review thread reports that `WorkerHeartbeatRepository.record_heartbeat` performs a
read-then-insert for missing heartbeat rows. Under PostgreSQL `READ COMMITTED`, two
first heartbeat writers for the same `worker_id` can both observe no row and then race
on the primary-key insert.

Scope is limited to making heartbeat recording atomic for PostgreSQL while preserving
the existing non-PostgreSQL ORM fallback and public repository behavior.

## Requirements Checklist

- Verify the reviewer claim against `src/awf/db/repositories/system_repo.py`.
- Add a focused regression test proving concurrent first heartbeat writes for the same
  worker do not raise an integrity error and leave one heartbeat row.
- Use PostgreSQL `INSERT ... ON CONFLICT (worker_id) DO UPDATE` for atomic heartbeat
  recording.
- Preserve existing update semantics: update `node_id`, `last_heartbeat_at`, and
  `poll_interval_seconds` on existing rows without replacing `started_at`.
- Run only targeted checks for the changed behavior; AWF/GitHub owns broad validation.
- Commit the scoped fix locally on the current AWF branch.

## Implementation Steps

1. Add a PostgreSQL heartbeat upsert statement helper using the existing repository
   dialect-helper pattern.
2. Update `WorkerHeartbeatRepository` to resolve the session dialect and use the
   upsert helper when available.
3. Keep the current read/update/insert ORM fallback when the upsert helper is
   unavailable.
4. Add focused unit coverage for the generated upsert SQL and the concurrent
   first-write regression.
5. Run the focused test(s) that cover the new behavior and any narrow lint/type check
   needed for touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py src/awf/db/repositories/system_repo.py tests/unit/db/test_worker_heartbeats.py`
  passes.
- Full AWF/GitHub validation is not run in-agent per the workspace contract.
