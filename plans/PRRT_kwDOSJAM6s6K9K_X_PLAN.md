# PRRT_kwDOSJAM6s6K9K_X Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6K9K_X` reports that the post-agent
`ComposeExecCleanupError` handler repairs `core.hooksPath` and rethrows without
checking whether the agent left `HEAD` pointing at an object outside the
canonical mirror. That can preserve a poisoned shared mirror ref for sibling
workspaces.

Scope is limited to the executor post-agent cleanup-failure path and focused
regression coverage for that path.

## Requirements Checklist

- Verify the current post-agent cleanup-failure path against the code.
- Preserve existing cleanup-failure behavior: workspace still fails with
  `EXEC_PROCESS_CLEANUP_FAILED`.
- Before propagating a post-agent cleanup failure, verify whether `HEAD`'s
  commit object exists using Git object lookup overrides stripped.
- If `HEAD` is missing, run existing missing-HEAD recovery and verify the
  recovered post-agent commit before failure handling continues.
- Add focused regression coverage for the missing-HEAD cleanup-failure path.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests.

## Implementation Steps

1. Import the existing `verify_head_object_exists` helper into
   `execution_flow.py`.
2. Add a local post-agent cleanup-failure helper in `execute` that repairs
   mirror hooks, verifies `HEAD`, and invokes existing recovery/verification
   helpers when needed.
3. Replace the post-agent `ComposeExecCleanupError` handler to use that helper.
4. Add a focused unit regression test in the executor error-recovery test area.
5. Run the specific new test or the narrow touched test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_006.py -q`

Pass criteria: targeted tests pass. Full AWF/GitHub validation remains managed
by AWF after agent completion.
