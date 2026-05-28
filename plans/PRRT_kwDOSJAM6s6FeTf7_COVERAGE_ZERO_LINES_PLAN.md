# PRRT_kwDOSJAM6s6FeTf7 Coverage Zero Lines Plan

## Problem Statement and Scope

The coverage threshold helper accepts a report with zero valid lines and
non-zero valid branches, but `main()` prints `totals.line_percent` after the
handled XML validation block. That raises `ValueError` and produces a Python
traceback instead of a clean coverage summary.

Scope is limited to:

- `scripts/check_coverage_threshold.py`
- `tests/unit/scripts/test_check_coverage_threshold.py`

## Requirements Checklist

- [ ] Add a regression test for branch-only coverage reports with
      `lines-valid="0"`.
- [ ] Preserve combined coverage threshold behavior for line and branch totals.
- [ ] Print `line=n/a` instead of raising when no line opportunities exist.
- [ ] Keep invalid coverage XML errors handled by the existing `::error`
      path.

## Implementation Steps

1. Add a focused failing regression test for a branch-only coverage report.
2. Run the new test and confirm it fails on the existing traceback behavior.
3. Update the coverage summary formatting to treat zero valid lines as an
   optional percentage.
4. Run focused tests and lint for the touched script/test files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py::test_checker_reports_branch_only_coverage_without_traceback -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py -q`
- `uv run --python 3.12 --extra dev ruff check scripts/check_coverage_threshold.py tests/unit/scripts/test_check_coverage_threshold.py`

Full AWF/GitHub validation is managed by AWF after agent completion.
