# PRRT_kwDOSJAM6s6Dfjod Continue-On-Error False Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Dfjod_CONTINUE_ON_ERROR_FALSE_PLAN.md`

## Requirement Status

- Add regression coverage proving that adding explicit `continue-on-error:
  false` to an existing step is a no-op for quality gate classification:
  Complete.
- Add regression coverage proving that removing explicit `continue-on-error:
  false` from an existing step is also a no-op: Complete.
- Preserve the existing block on dynamic `continue-on-error` expression changes:
  Complete.
- Keep the existing allowance for removing/enforcing a previously true
  `continue-on-error`: Complete.
- Run the narrow quality gate tests and style checks for touched files:
  Complete.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6Dfjod_CONTINUE_ON_ERROR_FALSE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Dfjod_CONTINUE_ON_ERROR_FALSE_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_absent_and_false_continue_on_error_are_equivalent -q`
  failed before the production fix with both absent/false transition cases
  returning one violation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_absent_and_false_continue_on_error_are_equivalent tests/unit/control/test_quality_gates.py::test_workflow_continue_on_error_expression_change_is_blocked tests/unit/control/test_quality_gates.py::test_workflow_setting_pytest_continue_on_error_false_is_allowed -q`
  passed after the production fix with 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with 288 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passed.

## Remaining Gaps

None.
