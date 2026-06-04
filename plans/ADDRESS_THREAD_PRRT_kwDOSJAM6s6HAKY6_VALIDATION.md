# Address Thread PRRT_kwDOSJAM6s6HAKY6 Validation

Plan reference: `ADDRESS_THREAD_PRRT_kwDOSJAM6s6HAKY6_PLAN.md`

## Requirement Status

- Verify the reviewer claim against `src/awf/db/repositories/system_repo.py`:
  Complete. The original method selected the heartbeat row, inserted when missing,
  and flushed, which allowed concurrent first writers to race on the primary key.
- Add a focused concurrent first-write regression test:
  Complete. `tests/unit/db/test_worker_heartbeats.py` forces the old read path to
  synchronize two missing-row reads and verifies concurrent writes leave one row
  without raising.
- Use PostgreSQL `INSERT ... ON CONFLICT (worker_id) DO UPDATE`:
  Complete. `_worker_heartbeat_upsert_stmt` builds the PostgreSQL upsert and
  `WorkerHeartbeatRepository.record_heartbeat` uses it when the resolved dialect is
  PostgreSQL.
- Preserve existing update semantics:
  Complete. The conflict update changes `node_id`, `last_heartbeat_at`,
  `poll_interval_seconds`, and `updated_at`; it does not replace `started_at`.
- Run only targeted checks:
  Complete. Full AWF/GitHub validation was not run in-agent per the workspace
  contract.
- Commit the scoped fix locally:
  Complete. The scoped fix and validation artifacts are included in the local
  commit for this thread.

## Evidence

Files changed:

- `src/awf/db/repositories/base.py`
- `src/awf/db/repositories/system_repo.py`
- `tests/unit/db/test_worker_heartbeats.py`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6HAKY6_PLAN.md`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6HAKY6_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q`
  passed with `2 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py src/awf/db/repositories/system_repo.py tests/unit/db/test_worker_heartbeats.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/base.py src/awf/db/repositories/system_repo.py`
  passed.

Full AWF/GitHub validation is managed after agent completion and was intentionally
not executed here.
