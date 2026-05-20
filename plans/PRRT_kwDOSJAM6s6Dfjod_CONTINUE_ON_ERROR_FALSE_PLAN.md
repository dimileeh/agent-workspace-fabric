# PRRT_kwDOSJAM6s6Dfjod Continue-On-Error False Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6Dfjod` reports that protected workflow
classification treats adding or removing explicit `continue-on-error: false` as
a protected quality-gate change even though GitHub Actions defaults the absent
key to false.

Scope is limited to the workflow quality gate classifier and focused unit
coverage in `tests/unit/control/test_quality_gates.py`.

## Requirements Checklist

- Add regression coverage proving that adding explicit `continue-on-error:
  false` to an existing step is a no-op for quality gate classification.
- Add regression coverage proving that removing explicit `continue-on-error:
  false` from an existing step is also a no-op.
- Preserve the existing block on dynamic `continue-on-error` expression changes.
- Keep the existing allowance for removing/enforcing a previously true
  `continue-on-error`.
- Run the narrow quality gate tests and style checks for touched files.

## Implementation Steps

1. Add a failing regression test for absent/false `continue-on-error`
   equivalence.
2. Confirm the regression fails against the current classifier.
3. Add a small helper that recognizes only unset or literal false
   `continue-on-error` values as disabled defaults.
4. Use that helper to suppress no-op absent/false transitions while preserving
   the existing true-removal and dynamic-expression handling.
5. Re-run the focused regression, the quality gate unit file, and lint checks.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_absent_and_false_continue_on_error_are_equivalent -q`
  fails before the production fix and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_continue_on_error_expression_change_is_blocked tests/unit/control/test_quality_gates.py::test_workflow_setting_pytest_continue_on_error_false_is_allowed -q`
  passes after the production fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
