# Fix Pass HEAD Reason Plan

## Problem Statement and Scope

An unresolved PR review thread reports that the pre-push validation fix-pass
handler logs `_MonitorHeadObjectMissingError.reason_code` but returns a hardcoded
`HEAD_OBJECT_MISSING_UNRECOVERABLE` reason. This can misreport a more specific
missing-HEAD reason raised by the commit sink after a fix pass.

Scope is limited to preserving the exception reason code in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and adding focused
regression coverage for that behavior.

## Requirements Checklist

- Add a focused regression test that fails when a fix-pass
  `_MonitorHeadObjectMissingError` reason code is flattened.
- Update the fix-pass missing-HEAD handler to return `exc.reason_code`.
- Keep existing messages and unrelated exception handling unchanged.
- Run only targeted validation for the changed behavior.

## Implementation Steps

1. Add a unit test around `_run_pre_push_validation_with_fix_passes` that raises
   `_MonitorHeadObjectMissingError` with a distinct custom reason code from the
   mocked fix pass.
2. Confirm the new test fails against the current implementation.
3. Change the handler to pass through `exc.reason_code`.
4. Re-run the focused test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k head_object_missing`
  - Passes after the code change.
  - Full AWF/GitHub validation is left to AWF after agent completion.
