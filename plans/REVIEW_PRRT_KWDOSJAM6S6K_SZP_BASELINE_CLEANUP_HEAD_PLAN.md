# Baseline Cleanup Missing HEAD Recovery Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K_Szp` reports that a `ComposeExecCleanupError`
raised after baseline coverage can bypass the missing-HEAD verification/recovery
used by nearby cleanup failure paths. If the baseline coverage command commits
with a private object directory before cleanup fails, the workspace branch ref
can point at an object missing from the canonical mirror.

Scope is limited to `src/awf/control/executor/execution_flow.py` and focused
regression coverage for this baseline cleanup branch.

## Requirements Checklist

- Verify the actual baseline cleanup handler does not already perform missing
  HEAD verification/recovery.
- Add missing-HEAD verification/recovery before re-raising baseline
  `ComposeExecCleanupError`.
- Preserve existing cleanup-failure behavior: mirror hooks are repaired first,
  and the outer cleanup failure path still marks the workspace failed.
- Add focused regression coverage for the baseline cleanup branch.
- Run only targeted tests/checks for changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a baseline cleanup recovery helper mirroring setup cleanup recovery with
   a baseline-specific stage name.
2. Invoke that helper in the baseline coverage `ComposeExecCleanupError` handler
   after mirror hook repair and before re-raising.
3. Add a focused unit test that simulates baseline coverage cleanup failure,
   missing HEAD verification failure, recovery success, and final cleanup
   failure marking.
4. Run the targeted unit test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_baseline_cleanup_recovery.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
