# PRRT_kwDOSJAM6s6HAfgq Once Heartbeat Plan

## Problem Statement And Scope

The review thread reports that `awf worker --once` records a heartbeat at the
start of `ControlWorker.run_once()` but then drains long-running execution tasks
through `wait_for_execution_tasks()` without starting the background heartbeat
loop. During that drain, `/readyz` can mark an active worker missing or stale.

Scope is limited to keeping worker heartbeats fresh while `wait_for_execution_tasks()`
waits for ready execution or PR-monitor resume tasks started by the worker.

## Requirements

- [ ] Preserve the initial `run_once()` heartbeat write behavior.
- [ ] While `wait_for_execution_tasks()` is waiting on still-running execution
  tasks, periodically refresh heartbeat using the existing safe heartbeat writer.
- [ ] Preserve prompt removal of completed/cancelled tasks and exception
  propagation from failed execution tasks.
- [ ] Add focused regression coverage for the once-mode drain heartbeat behavior.
- [ ] Do not run broad AWF/GitHub-owned validation; record focused local checks.

## Implementation Steps

1. Add a focused unit test proving `wait_for_execution_tasks()` writes another
   heartbeat while an active execution task remains blocked.
2. Run that test to confirm it fails before implementation.
3. Update `ControlWorker.wait_for_execution_tasks()` to wait with the existing
   heartbeat write interval as a timeout and call `_record_heartbeat_safely()`
   when no task has completed during that interval.
4. Re-run the focused worker heartbeat tests.

## Verification Commands

Focused checks only:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py tests/unit/control/test_worker_stop.py
```

Pass criteria: the focused tests pass, and ruff reports no diagnostics for the
changed Python files. Full AWF/GitHub validation is managed after agent completion.
