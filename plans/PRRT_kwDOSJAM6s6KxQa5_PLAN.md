# PRRT_kwDOSJAM6s6KxQa5 Plan

## Problem Statement and Scope

The review thread reports that a post-validation conformance cleanup failure can overwrite the already-served satisfied `conformance.json` with a stale worktree report. The scope is limited to the cleanup-failure terminal path after `_run_post_validation_conformance_check` returns `POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED`.

## Requirements Checklist

- Verify the cleanup-failure path does not re-deposit conformance artifacts from the stale worktree report.
- Preserve the terminal FAILED status and infrastructure failure classification for cleanup residue.
- Add a focused regression test for the no-redeposit behavior.
- Run only targeted validation for the changed behavior; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add a regression assertion around the cleanup-failure branch in `run_validation_and_fix_cycle`.
2. Change only the cleanup-failure terminal branch to call `_mark_failed` directly after finishing pending validate operations, because `_run_post_validation_conformance_check` already deposits the correct served artifacts before cleanup.
3. Keep all other terminal failure branches on `_mark_failed_preserving_planning_artifacts`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py -q`
- Pass criteria: the focused test file passes, including the cleanup-failure regression.
