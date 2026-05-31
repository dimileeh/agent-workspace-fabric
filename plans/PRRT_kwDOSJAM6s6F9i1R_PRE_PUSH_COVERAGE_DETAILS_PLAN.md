# PRRT_kwDOSJAM6s6F9i1R Pre-Push Coverage Details Plan

## Problem Statement And Scope

The PR review thread reports that pre-push validation failure details can report
`validation_reason_code=COVERAGE_BELOW_THRESHOLD` while also reporting
`failing_returncode=0` when the coverage command succeeded but the coverage
policy gate failed. Scope is limited to the PR monitor pre-push validation
failure details payload and focused regression coverage for that behavior.

## Requirements Checklist

- Reproduce the coverage-only policy failure with a successful coverage command.
- Preserve existing failing command diagnostics for real command failures.
- Avoid emitting `failing_command` or `failing_returncode` when the selected
  command result is successful and the failure is policy-only coverage.
- Keep changes scoped to PR monitor pre-push validation behavior and its tests.
- Run only focused local checks; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Update the existing coverage-failure pre-push validation test to assert that
   no successful command return code is reported as the failing return code.
2. Confirm the focused regression fails before implementation when practical.
3. Update `failure_details()` to include command diagnostics only for command
   results that are actually failed according to `ValidationCommandResult.ok`.
4. Run the focused test module or the narrow test that covers the regression.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k coverage_failure`
  - Passes with the regression assertion.
- Full AWF/GitHub validation is intentionally not run in the agent phase per
  the workspace contract.
