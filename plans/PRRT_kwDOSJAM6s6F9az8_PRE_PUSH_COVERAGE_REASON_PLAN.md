# PRRT_kwDOSJAM6s6F9az8 Pre-Push Coverage Reason Plan

## Problem Statement And Scope

An unresolved review thread reports that PR monitor pre-push validation can
persist and surface `VALIDATION_OK` when profile phase commands pass but the
separate profile coverage gate fails. The scope is limited to preserving the
coverage-derived validation reason code for pre-push validation failures while
keeping the existing mixed 127/non-127 command precedence behavior.

## Requirements Checklist

- Add a regression test for a pre-push coverage failure after successful
  `post_agent`/`validate` phases.
- Ensure the returned push failure details expose the coverage reason code,
  such as `COVERAGE_BELOW_THRESHOLD`, not the successful command reason.
- Ensure the persisted validation run reason code uses the coverage reason.
- Preserve existing toolchain-missing and mixed-failure precedence behavior.
- Use only focused validation commands; AWF/GitHub own broad validation after
  this agent phase.

## Implementation Steps

1. Add a focused regression test in `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`.
2. Run the new focused test and confirm it fails under the current helper.
3. Update `_pre_push_validation_reason_code` in
   `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` to delegate
   coverage-owned failures to `_validation_run_reason_code`.
4. Re-run the focused test and a nearby focused pre-push validation subset.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py::test_pre_push_validation_coverage_failure_persists_coverage_reason_code -q`
  - Passes after the implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  - Passes after the implementation.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
