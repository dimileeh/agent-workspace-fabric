# Review 4620209013 Worker Heartbeat Cleanup Validation

Plan reference: `plans/REVIEW_4620209013_WORKER_HEARTBEAT_CLEANUP_PLAN.md`

## Requirement Status

- Add a regression proving a failed heartbeat task does not crash
  `run_forever()` during shutdown cleanup: Complete.
- Preserve cancellation behavior for a normally running heartbeat task:
  Complete.
- Add a stale heartbeat cleanup path with a bounded delete so old worker IDs do
  not accumulate indefinitely: Complete.
- Add focused repository coverage proving fresh rows are preserved and stale
  rows are pruned: Complete.
- Keep changes minimal and avoid unrelated refactors: Complete.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `src/awf/db/repositories/system_repo.py`
- `tests/unit/control/test_worker_stop.py`
- `tests/unit/db/test_worker_heartbeats.py`
- `plans/REVIEW_4620209013_WORKER_HEARTBEAT_CLEANUP_PLAN.md`
- `plans/REVIEW_4620209013_WORKER_HEARTBEAT_CLEANUP_VALIDATION.md`

TDD failures observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q -k "shutdown_ignores_failed_heartbeat_task"` failed because `run_forever()` re-raised `AssertionError("unreachable DB retry state")` while awaiting the completed heartbeat task in `finally`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q -k "prune_stale or concurrent_first_writes"` failed because `WorkerHeartbeatRepository` had no `prune_stale` method.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q -k "shutdown_ignores_failed_heartbeat_task or exits_when_stop_requested or prunes_stale_worker_heartbeats"`: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q -k "prune_stale or concurrent_first_writes"`: passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py src/awf/db/repositories/system_repo.py tests/unit/control/test_worker_stop.py tests/unit/db/test_worker_heartbeats.py`: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py tests/unit/db/test_worker_heartbeats.py -q`: passed, `12 passed`.

Full AWF/GitHub validation was not run in the agent phase; AWF owns the broad
post-agent validation suite and merge-gating provenance.
