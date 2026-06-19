# Mirror Hooks Repair Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6K5xQY` reports that `execution_flow.py` repairs a
poisoned shared mirror `core.hooksPath` only before profile setup. Setup,
preflight, coverage, or another workspace can poison the shared mirror again
before the main agent launch or AWF's post-agent commit, allowing commits to run
with hooks disabled.

Scope is limited to the executor flow and focused regression tests for the
reported repair timing.

## Requirements Checklist

- Re-check and fail closed on mirror hooks repair immediately before the main
  agent can run and create self-commits.
- Re-check and fail closed before AWF runs its post-agent `git commit`.
- Preserve existing before-profile-setup behavior and recovery failure handling.
- Add focused regression coverage for the new repair timing.
- Run only targeted tests for the changed behavior; full AWF/GitHub validation
  remains managed after agent completion.

## Implementation Steps

1. Add a small local helper in `execute` that performs the mirror hooks repair
   for a named stage and marks the workspace failed with the stage-specific
   message if repair fails.
2. Replace the existing inline before-setup repair block with the helper.
3. Call the helper before `_run_agent_task_with_optional_planning`.
4. Call the helper immediately before the `_run_commit()` invocation.
5. Add focused unit tests in `tests/unit/control/test_executor_mirror_hooks_path.py`.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`

Pass criteria: the focused test file passes, and no broad validation suite is
run inside this agent phase.
