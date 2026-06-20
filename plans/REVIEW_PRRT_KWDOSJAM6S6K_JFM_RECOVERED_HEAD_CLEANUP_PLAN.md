# REVIEW PRRT_kwDOSJAM6s6K-JFM Recovered Head Cleanup Plan

## Problem Statement and Scope

An unresolved PR review thread reports that recovered missing-HEAD commits are
left checked out when recovered-head diff inspection fails before validation.
The scope is limited to `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
and focused regression coverage for those recovered-head failure returns.

## Requirements Checklist

- Verify recovered missing-HEAD diff failures restore `recovery_head` before returning.
- Cover both recovered changed-path diff failure and recovered protected-scope committed diff failure.
- Preserve the existing fail-closed `PROTECTED_SCOPE_DIFF_UNAVAILABLE` result and avoid starting validation.
- Keep validation local and focused; broad AWF/GitHub validation remains post-agent.

## Implementation Steps

1. Add/adjust focused tests that fail until cleanup runs for both recovered-head diff-unavailable returns.
2. Add minimal cleanup calls before those early returns.
3. Run only targeted tests for the changed behavior.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py::test_pre_push_validation_recovered_head_diff_failure_blocks_validation tests/unit/runtime/test_pr_monitor_pre_push_validation_edges_part_002.py::test_pre_push_validation_recovered_head_committed_diff_error_blocks_validation -q`
- Pass criteria: both targeted regression tests pass and still assert validation did not run.
