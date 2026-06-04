# Review PRRT_kwDOSJAM6s6HAEj9 Heartbeat Throttle Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6HAEj9` reports that `ControlWorker.run_once()`
can call `_record_heartbeat_safely()` in rapid succession while processing a
large backlog, causing worker heartbeat DB writes to occur more frequently than
the configured heartbeat write interval.

Scope is limited to throttling successful heartbeat writes from
`ControlWorker._record_heartbeat_safely()` and adding focused regression
coverage.

## Requirements Checklist

- Add a regression test showing repeated safe heartbeat calls inside the write
  interval record only one heartbeat.
- Preserve the first heartbeat write for a new worker.
- Preserve the existing behavior that heartbeat write failures are swallowed and
  logged by `_record_heartbeat_safely()`.
- Keep the write interval derived from
  `worker_heartbeat_write_interval_seconds(poll_interval_seconds)`.
- Keep changes scoped to worker heartbeat implementation, focused tests, and
  plan/validation artifacts.
- Run only focused validation; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Inspect the current worker heartbeat implementation and tests.
2. Add a focused regression test for throttling repeated safe heartbeat calls.
3. Run the new test and confirm it fails before implementation when practical.
4. Add per-worker monotonic state and skip heartbeat writes until the configured
   write interval has elapsed after the last successful write.
5. Run focused tests for `tests/unit/control/test_worker_stop.py`.
6. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py::test_record_heartbeat_safely_throttles_repeated_writes -q`
  - Fails before implementation.
  - Passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py tests/unit/control/test_worker_stop.py`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally not run during the agent phase per
the workspace contract.
