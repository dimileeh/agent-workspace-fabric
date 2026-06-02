# PRRT_kwDOSJAM6s6GUcB6 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GUcB6_PLAN.md`

## Requirement Status

- Add a regression test showing a blocked planning-scope auto-retry is retried
  after terminal runtime release: Complete.
- Keep `retry_workspace_row` as the code path that creates the replacement
  workspace: Complete.
- Record an auto-retry requested event when the deferred retry succeeds:
  Complete.
- Record an auto-retry failed event if the deferred retry is still blocked by
  normal retry errors: Complete; the shared request helper preserves the
  existing failed-event branch for both immediate and deferred attempts.
- Avoid creating duplicate planning auto-retries if a retry was already
  requested: Complete; the resume path only proceeds when the latest relevant
  planning retry event is the terminal-runtime blocked marker.
- Run only focused checks for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/planning_ops.py`
- `src/awf/control/worker/cleanup.py`
- `tests/unit/control/test_executor_planning_auto_retry_transactions.py`
- `plans/PRRT_kwDOSJAM6s6GUcB6_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GUcB6_VALIDATION.md`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q`
  - Result: passed, `5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_040.py::TestTerminalRuntimeReleasePart001::test_release_stops_terminal_failed_workspace_runtime_preserving_volumes_and_worktree -q`
  - Result: passed, `1 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py tests/unit/control/test_executor_planning_auto_retry_transactions.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py`
  - Result: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, timeouts, and merge gating after agent completion.
