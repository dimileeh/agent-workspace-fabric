# PRRT_kwDOSJAM6s6KxEcu Plan

## Problem Statement and Scope

An unresolved PR review thread reports that post-validation conformance report cleanup can return success after `Path.unlink()` raises, because the dirty-path check only runs when `unlink_succeeded` is true. The scope is limited to `src/awf/control/executor/planning_conformance.py` cleanup behavior and a focused regression test.

## Requirements Checklist

- Verify the review claim against the local implementation before editing.
- Add a regression test for an unlink exception where the report path remains dirty.
- Preserve existing successful cleanup behavior and existing cleanup failure reason codes.
- Keep validation focused; do not run broad AWF/GitHub-owned validation.

## Implementation Steps

1. Add a focused unit test beside the existing post-validation cleanup dirty-index regression.
2. Update the cleanup branch to inspect `_report_path_is_dirty()` after `unlink()` attempts, including exception cases.
3. Return `_build_report_cleanup_failure()` when the report path remains dirty.
4. Run the narrow test selection that covers the new regression.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py -q -k "satisfied_post_validation_conformance_report_fails_when_unlink"`

Pass criteria: the targeted cleanup tests pass. Full AWF/GitHub validation is intentionally not run during the agent phase.
