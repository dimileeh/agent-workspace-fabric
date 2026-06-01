# PRRT_kwDOSJAM6s6F9i1R Pre-Push Coverage Details Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F9i1R_PRE_PUSH_COVERAGE_DETAILS_PLAN.md`

## Requirement Status

- Reproduce the coverage-only policy failure with a successful coverage command:
  Complete. The updated regression failed before implementation because
  `failure_details()` emitted `failing_command` and `failing_returncode=0`.
- Preserve existing failing command diagnostics for real command failures:
  Complete. Focused neighboring tests covering validation and toolchain command
  failures still pass.
- Avoid emitting `failing_command` or `failing_returncode` when the selected
  command result is successful and the failure is policy-only coverage:
  Complete. `failure_details()` now guards command detail emission on
  `not first_failure.ok`.
- Keep changes scoped to PR monitor pre-push validation behavior and its tests:
  Complete. Code changes are limited to
  `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and
  `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`.
- Run only focused local checks:
  Complete. Full AWF/GitHub validation was not run in the agent phase because
  AWF owns broad validation after agent completion.

## Evidence

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k coverage_failure`
  failed with `failing_returncode: 0` present in `result.details`.
- Passing focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k coverage_failure`
  passed (`1 passed, 21 deselected`).
- Passing focused neighboring detail tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k "coverage_failure or validation_failure_does_not_push or toolchain_missing_bypasses_fix_pass or fix_pass_commit_fail"`
  passed (`4 passed, 18 deselected`).
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  passed.
