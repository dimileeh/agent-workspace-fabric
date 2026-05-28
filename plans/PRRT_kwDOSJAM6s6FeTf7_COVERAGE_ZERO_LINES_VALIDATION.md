# PRRT_kwDOSJAM6s6FeTf7 Coverage Zero Lines Validation

Plan reference: `PRRT_kwDOSJAM6s6FeTf7_COVERAGE_ZERO_LINES_PLAN.md`

## Requirement Status

- Add a regression test for branch-only coverage reports with
  `lines-valid="0"`: Complete.
  - Evidence: `tests/unit/scripts/test_check_coverage_threshold.py` adds
    `test_checker_reports_branch_only_coverage_without_traceback`.
- Preserve combined coverage threshold behavior for line and branch totals:
  Complete.
  - Evidence: the regression expects `combined=99.00%` and return code `0`
    for 99 covered branches out of 100 with no valid lines.
- Print `line=n/a` instead of raising when no line opportunities exist:
  Complete.
  - Evidence: `scripts/check_coverage_threshold.py` now formats line coverage
    through `_format_optional_percent` when `lines_valid == 0`.
- Keep invalid coverage XML errors handled by the existing `::error` path:
  Complete.
  - Evidence: the full focused script test module still passes, including
    `test_checker_reports_invalid_coverage_xml`.

## Verification

- Confirmed failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py::test_checker_reports_branch_only_coverage_without_traceback -q`
  - Failed with return code `1` from the script and `ValueError: coverage
    report has no measurable line or branch opportunities`.
- Confirmed fixed regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py::test_checker_reports_branch_only_coverage_without_traceback -q`
  - Passed.
- Focused test module:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py -q`
  - Passed: 9 tests.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check scripts/check_coverage_threshold.py tests/unit/scripts/test_check_coverage_threshold.py`
  - Passed.

Full AWF/GitHub validation is managed by AWF after agent completion.
