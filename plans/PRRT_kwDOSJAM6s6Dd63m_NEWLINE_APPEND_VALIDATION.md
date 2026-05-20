# PRRT_kwDOSJAM6s6Dd63m Newline Append Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Dd63m_NEWLINE_APPEND_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing `pytest && python -m unittest\ncurl ...` is rejected.
- Complete: Confirmed the regression failed before implementation.
- Complete: Rejected validation-run append suffixes containing `\n` or `\r` before shell tokenization.
- Complete: Ran focused and full quality-gate unit tests plus lint on touched Python files.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6Dd63m_NEWLINE_APPEND_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Dd63m_NEWLINE_APPEND_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_validation_run_preservation_allows_only_safe_validation_appends -q`
  - Failed before implementation on the new newline regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_validation_run_preservation_allows_only_safe_validation_appends tests/unit/control/test_quality_gates.py::test_private_shell_and_validation_helpers_cover_remaining_parser_edges -q`
  - Passed: 23 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passed: 267 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Passed.

## Gaps

None.
