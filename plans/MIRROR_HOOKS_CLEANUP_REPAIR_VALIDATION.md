# Mirror Hooks Cleanup Repair Validation

Plan reference: `plans/MIRROR_HOOKS_CLEANUP_REPAIR_PLAN.md`

## Requirement Status

- Add a focused regression test proving the executor attempts mirror hook repair
  after an agent/planning `ComposeExecCleanupError`: Complete.
- Preserve existing failure handling: deposit planning artifacts, mark workspace
  failed with `EXEC_PROCESS_CLEANUP_FAILED`, and return: Complete for the scoped
  mirror-hook regression. The test asserts the cleanup failure remains the
  recorded failure reason when after-cleanup repair fails.
- Avoid masking the original cleanup failure if the best-effort mirror repair
  itself fails: Complete.
- Keep the code change minimal and scoped to the reviewed behavior: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/execution_flow.py`
- `tests/unit/control/test_executor_mirror_hooks_path.py`
- `plans/MIRROR_HOOKS_CLEANUP_REPAIR_PLAN.md`
- `plans/MIRROR_HOOKS_CLEANUP_REPAIR_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_agent_cleanup_failure -q`
  - First run before implementation failed because only two repair calls were
    made.
  - Final run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Passed: `5 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py tests/unit/control/test_executor_mirror_hooks_path.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase per the workspace
contract; AWF owns broad validation after completion.
