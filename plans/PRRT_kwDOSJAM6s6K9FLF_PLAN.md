# PRRT_kwDOSJAM6s6K9FLF Plan

## Problem Statement and Scope

The PR review thread reports that the CI repair path catches
`_MonitorHeadObjectMissingError` but returns the fixed
`HEAD_OBJECT_MISSING_UNRECOVERABLE` reason instead of the exception's
`reason_code`. Scope is limited to preserving the exception-provided reason in
CI repair results and adding a focused regression test.

## Requirements Checklist

- Verify the review claim against the actual CI repair code.
- Preserve `_MonitorHeadObjectMissingError.reason_code` in `_run_ci_fix`
  failure results.
- Add or update focused unit coverage proving a non-default missing-HEAD reason
  survives the CI repair path.
- Run only targeted validation for the changed behavior; full AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Update `src/awf/runtime/pr_monitor_runner/ci_ops.py` to return
   `exc.reason_code` in the `_MonitorHeadObjectMissingError` handler.
2. Update the focused CI fix missing-HEAD test to raise and assert a custom
   reason code.
3. Run the targeted unit test for the changed path.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k ci_fix_catches_head_object_missing_error`
  - Passes and proves the CI repair path preserves the exception reason.
