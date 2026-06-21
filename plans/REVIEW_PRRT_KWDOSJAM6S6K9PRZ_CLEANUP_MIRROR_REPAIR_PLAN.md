# Cleanup Mirror Repair Review Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K9PrZ` reports that
`repair_mirror_hooks_path_after_agent_cleanup_failure` logs mirror hook repair
failures but lets the caller continue reporting the original cleanup failure.
This can leave a poisoned shared mirror in service after cleanup proved repair
did not succeed.

Scope is limited to the executor mirror hooks repair path after agent cleanup
failure and focused regression coverage for that behavior.

## Requirements Checklist

- Verify the review claim against local code and tests.
- Add/update focused regression coverage so a failed mirror repair after agent
  cleanup failure fails closed with the mirror-repair reason.
- Keep successful cleanup-repair behavior unchanged.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests for touched
  behavior.

## Implementation Steps

1. Update the existing cleanup-failure mirror-repair unit test to expect the
   mirror-repair failure reason instead of the original cleanup failure when
   repair itself fails.
2. Change the cleanup-failure repair helper/caller so logged repair failures
   abort the executor path after marking the workspace failed with the mirror
   repair reason.
3. Run the focused unit tests covering mirror hooks repair behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Passes with the updated regression assertions.

Full AWF/GitHub validation remains managed by AWF after agent completion.
