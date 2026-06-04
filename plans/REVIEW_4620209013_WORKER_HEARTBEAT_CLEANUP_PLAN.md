# Review 4620209013 Worker Heartbeat Cleanup Plan

## Problem Statement And Scope

Review-level comment `issue:4620209013` reports two worker heartbeat edge cases:

- `ControlWorker.run_forever()` can re-raise a completed heartbeat task's stored
  non-cancellation exception from the `finally` cleanup path.
- `worker_heartbeats` keeps one row per worker process lifetime with no stale-row
  cleanup path.

Scope is limited to worker heartbeat shutdown/pruning behavior and focused
regression tests. No broad AWF/GitHub-owned validation will be run during the
agent phase.

## Requirements Checklist

- Add a regression proving a failed heartbeat task does not crash
  `run_forever()` during shutdown cleanup.
- Preserve cancellation behavior for a normally running heartbeat task.
- Add a stale heartbeat cleanup path with a bounded delete so old worker IDs do
  not accumulate indefinitely.
- Add focused repository coverage proving fresh rows are preserved and stale
  rows are pruned.
- Keep changes minimal and avoid unrelated refactors.

## Implementation Steps

1. Add focused failing tests in `tests/unit/control/test_worker_stop.py` and
   `tests/unit/db/test_worker_heartbeats.py`.
2. Update `ControlWorker.run_forever()` heartbeat-task cleanup to log and
   swallow non-cancellation heartbeat task failures during shutdown.
3. Add a bounded stale-row pruning method to `WorkerHeartbeatRepository`.
4. Invoke stale-row pruning from the worker heartbeat path using a monotonic
   guard so cleanup is periodic, not every heartbeat.
5. Run targeted tests for the changed behavior only.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q -k "shutdown_ignores_failed_heartbeat_task or exits_when_stop_requested or prunes_stale_worker_heartbeats"`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_worker_heartbeats.py -q -k "prune_stale or concurrent_first_writes"`

Full AWF/GitHub validation is managed by AWF after agent completion.
