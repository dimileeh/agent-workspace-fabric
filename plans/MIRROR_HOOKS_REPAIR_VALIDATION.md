# Mirror Hooks Repair Validation

Plan reference: `plans/MIRROR_HOOKS_REPAIR_PLAN.md`

## Requirement Status

- Complete: Re-check and fail closed before the main agent launch.
  - `execution_flow.execute` now calls mirror hooks repair again after the
    `agent_run` status recheck and before `_run_agent_task_with_optional_planning`.
- Complete: Re-check and fail closed before AWF's post-agent `git commit`.
  - The shared `_run_commit` callback repairs the mirror before each executor
    commit attempt, including repair retries that reuse the callback.
- Complete: Preserve existing before-profile-setup behavior and recovery
  failure handling.
  - The existing setup-stage failure message and recovery completion path remain
    covered by the existing regression.
- Complete: Add focused regression coverage.
  - Added tests for failure before agent launch and before post-agent commit.
- Complete: Run targeted validation only.
  - Full AWF/GitHub validation is intentionally left to AWF after agent
    completion per the workspace contract.

## Evidence

Files changed:

- `src/awf/control/executor/execution_flow.py`
- `tests/unit/control/test_executor_mirror_hooks_path.py`
- `plans/MIRROR_HOOKS_REPAIR_PLAN.md`
- `plans/MIRROR_HOOKS_REPAIR_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Passed: `4 passed in 0.69s`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py tests/unit/control/test_executor_mirror_hooks_path.py`
  - Passed: `All checks passed!`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/executor/execution_flow.py tests/unit/control/test_executor_mirror_hooks_path.py`
  - Passed: `2 files already formatted`

## Gaps

No planned gaps remain.
