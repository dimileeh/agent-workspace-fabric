# PRRT_kwDOSJAM6s6DaP8G Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DaP8G_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving a mismatched-status salvage event does not suppress stale-active-execution failure for the current status.
- Complete: Updated `_has_current_salvage_event` to filter on the salvage payload's `workspace_status`.
- Complete: Passed the current `WorkspaceStatus` through each salvage idempotency caller.
- Complete: Replaced the reviewed stale-failure salvage checks with an iterable collection while reusing the existing database session.
- Complete: Prepared the local change for a thread-specific conventional commit.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker_coverage_edges.py`
- `plans/PRRT_kwDOSJAM6s6DaP8G_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DaP8G_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_stale_active_execution_can_fail_ignores_salvage_for_other_status -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_stale_active_execution_can_fail_rejects_preserved_runtime tests/unit/control/test_worker_coverage_edges.py::test_stale_active_execution_can_fail_ignores_salvage_for_other_status tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_rewound_validation_salvage_waits_without_duplicate_when_slots_full -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker_coverage_edges.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

The new regression was first run before implementation and failed with `assert False` from `_stale_active_execution_can_fail`, confirming that the old unscoped salvage lookup incorrectly suppressed current-status stale failure.
