# Review PRRT_kwDOSJAM6s6K7VBN Mirror Hooks Plan

## Problem Statement and Scope

An unresolved review thread reports that unexpected non-`AgentRunError` and non-`ComposeExecCleanupError` failures raised from `_run_agent_task_with_optional_planning` can skip post-agent mirror hooks repair. The fix is limited to `src/awf/control/executor/execution_flow.py` and a focused regression test for that failure path.

## Requirements Checklist

- Confirm whether the unexpected agent-run exception path currently reaches mirror hooks repair.
- Add a regression test that fails without a repair after an unexpected agent-run exception.
- Repair mirror hooks after unexpected agent-run exceptions before the outer generic failure handler marks the workspace failed.
- Preserve existing `AgentRunError` salvage behavior and `ComposeExecCleanupError` cleanup behavior.
- Run only focused validation for the touched test behavior; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add a unit test in `tests/unit/control/test_executor_mirror_hooks_path.py` for an unexpected exception raised by `_run_agent_task_with_optional_planning`.
2. Run the focused test to confirm it fails before implementation when practical.
3. Update the inner agent-run `try` block in `execution_flow.py` to repair mirror hooks on unexpected exceptions, then re-raise to the existing outer handler.
4. Keep explicit `AgentRunError` and `ComposeExecCleanupError` paths behaviorally unchanged.
5. Re-run the focused test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`

Pass criteria: the targeted mirror hooks regression tests pass. Full AWF/GitHub validation is intentionally not run inside this agent phase.
