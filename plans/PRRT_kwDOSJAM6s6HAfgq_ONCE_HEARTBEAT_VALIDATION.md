# PRRT_kwDOSJAM6s6HAfgq Once Heartbeat Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6HAfgq_ONCE_HEARTBEAT_PLAN.md`

## Requirement Status

- Complete: Preserve the initial `run_once()` heartbeat write behavior.
  - Existing `test_run_once_records_worker_heartbeat` still passes.
- Complete: While `wait_for_execution_tasks()` is waiting on still-running
  execution tasks, periodically refresh heartbeat using the existing safe
  heartbeat writer.
  - `ControlWorker.wait_for_execution_tasks()` now waits with the configured
    heartbeat write interval as a timeout and calls `_record_heartbeat_safely()`
    when no tracked task has completed, or after completed tasks are processed
    while other execution tasks remain active.
- Complete: Preserve prompt removal of completed/cancelled tasks and exception
  propagation from failed execution tasks.
  - Existing wait/drain tests in `tests/unit/control/test_worker_stop.py` still
    pass.
- Complete: Add focused regression coverage for the once-mode drain heartbeat
  behavior.
  - Added `test_wait_for_execution_tasks_refreshes_heartbeat_while_draining_once_worker`.
- Complete: Do not run broad AWF/GitHub-owned validation.
  - Focused local checks listed below were run manually. The local commit hook
    also ran its configured checks during commit. Full AWF/GitHub validation is
    managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `tests/unit/control/test_worker_stop.py`
- `plans/PRRT_kwDOSJAM6s6HAfgq_ONCE_HEARTBEAT_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6HAfgq_ONCE_HEARTBEAT_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py::test_wait_for_execution_tasks_refreshes_heartbeat_while_draining_once_worker -q
# Failed before implementation: AssertionError, heartbeat await_count stayed at 1.
# Passed after implementation: 1 passed.

uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q
# Passed: 7 passed.

uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py tests/unit/control/test_worker_stop.py
# Passed: All checks passed.

git commit
# Local hooks passed during commit: trailing whitespace, EOF, large-file,
# merge-conflict, private-key, ruff check, ruff format --check, mypy.
```

## Gaps

No planned requirements remain partial or missing.
