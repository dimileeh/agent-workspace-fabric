# PRRT_kwDOSJAM6s6K9hDe Plan

## Problem Statement and Scope

The PR review thread reports that an agent `ComposeExecCleanupError` can be masked
by missing-HEAD recovery failure. In that path, recovery marks the workspace failed
with `GIT_OBJECT_MISSING`, then execution returns before the outer cleanup-failure
handler records `EXEC_PROCESS_CLEANUP_FAILED`.

Scope is limited to preserving the original cleanup failure as the terminal reason
for this cleanup-error path while keeping existing missing-HEAD recovery behavior for
non-cleanup errors unchanged.

## Requirements Checklist

- Verify the reported control flow against `src/awf/control/executor/execution_flow.py`.
- Add focused regression coverage for cleanup failure plus failed missing-HEAD recovery.
- Implement the smallest code change so cleanup failure remains terminal when recovery
  cannot repair HEAD after an agent cleanup error.
- Run only focused tests for the changed behavior.
- Document validation evidence in `plans/PRRT_kwDOSJAM6s6K9hDe_VALIDATION.md`.

## Implementation Steps

1. Read the execution-flow cleanup handler and missing-HEAD recovery helper.
2. Add or update a unit test that exercises agent cleanup failure, missing HEAD, and
   failed recovery.
3. Adjust the recovery call path to avoid preemptively terminal-failing with
   `GIT_OBJECT_MISSING` when a cleanup failure is already the primary error.
4. Run the focused unit test file or targeted test node.
5. Record validation evidence and commit the scoped changes.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py -q`
  passes.
- Full AWF/GitHub validation is intentionally not run during this agent phase;
  AWF owns broad validation after agent completion.
