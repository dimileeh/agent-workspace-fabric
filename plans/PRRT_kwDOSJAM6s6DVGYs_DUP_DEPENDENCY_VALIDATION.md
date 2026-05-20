# PRRT_kwDOSJAM6s6DVGYs Duplicate Dependency Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DVGYs_DUP_DEPENDENCY_PLAN.md`

## Requirement Status

- Complete: Detect removal of one duplicate dependency entry for the same
  normalized package name.
  - Evidence: Added
    `test_pyproject_duplicate_dependency_entry_deletion_is_blocked`, which
    failed against the original map-based classifier with zero violations, then
    passed after preserving counted dependency entries.
- Complete: Continue allowing additive dependency entries.
  - Evidence: Existing `test_pyproject_dependency_addition_is_allowed` remains
    green in the full quality-gates test module.
- Complete: Continue blocking changed dependency requirements.
  - Evidence: `_dependency_list_violations` still reports a changed dependency
    when an old raw requirement entry is absent but the normalized package name
    still exists with replacement entries.
- Complete: Preserve existing unsupported-format fail-closed behavior.
  - Evidence: Unsupported-format handling still returns a conservative
    violation when either old or new dependency parsing returns `None`.

## Files Changed

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DVGYs_DUP_DEPENDENCY_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DVGYs_DUP_DEPENDENCY_VALIDATION.md`

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_pyproject_duplicate_dependency_entry_deletion_is_blocked -q`
  - Initial run failed before the implementation with `len(violations) == 0`.
  - Final run passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passed: 63 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf tests`
  - Passed.

## Notes

The broader `uv run --python 3.12 --extra dev pytest tests/unit -q` run was
started as an extra check but stopped because it was still early in the suite
after several minutes. The planned pass criteria are satisfied by the targeted
quality-gates module, full ruff, and full mypy checks.
