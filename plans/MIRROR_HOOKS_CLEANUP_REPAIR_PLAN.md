# Mirror Hooks Cleanup Repair Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K6WMO` reports that executor mirror hook
repair runs before agent launch and before post-agent commit, but not after
`_run_agent_task_with_optional_planning()` raises `ComposeExecCleanupError`.
That can leave a shared mirror poisoned if the agent changed `core.hooksPath`
before AWF marks the workspace failed.

Scope is limited to the executor cleanup-error path in
`src/awf/control/executor/execution_flow.py` and focused unit coverage for that
path. No PR/GitHub writes, branch changes, pushes, or broad validation.

## Requirements Checklist

- Add a focused regression test proving the executor attempts mirror hook repair
  after an agent/planning `ComposeExecCleanupError`.
- Preserve existing failure handling: deposit planning artifacts, mark workspace
  failed with `EXEC_PROCESS_CLEANUP_FAILED`, and return.
- Avoid masking the original cleanup failure if the best-effort mirror repair
  itself fails.
- Keep the code change minimal and scoped to the reviewed behavior.

## Implementation Steps

1. Add a failing unit test in the existing mirror hooks regression test module.
2. Implement the minimal post-agent cleanup repair around the agent invocation.
3. Run the targeted test command for the affected test module or new test.
4. Record validation evidence in `plans/MIRROR_HOOKS_CLEANUP_REPAIR_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns broad validation after completion.
