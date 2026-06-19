# PRRT_kwDOSJAM6s6KxJt9 Plan

## Problem Statement and Scope

The PR review thread reports that recovered HEAD-object diff collection can fail without surfacing as an unrecoverable monitor failure. The scope is limited to the recovered-HEAD diff branch in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and its existing focused regression test.

## Requirements Checklist

- Verify the current implementation against the review thread.
- Treat recovered diff collection failure as a reason-coded unrecoverable HEAD-object error.
- Preserve the existing supply-chain policy refresh behavior for successful recovered diffs.
- Add or update a focused regression test for the changed failure behavior.
- Run only targeted validation for the touched test.

## Implementation Steps

1. Update the existing diff-failure regression to expect `_MonitorHeadObjectMissingError`.
2. Change the recovered diff failure branch to raise `_MonitorHeadObjectMissingError` with `_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON`.
3. Run the focused unit test covering this branch.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -k recovered_diff_fails -q`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally left to AWF after agent completion per the workspace contract.
