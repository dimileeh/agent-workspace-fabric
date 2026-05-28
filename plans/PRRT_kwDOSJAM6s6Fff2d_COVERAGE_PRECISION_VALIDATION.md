# PRRT_kwDOSJAM6s6Fff2d Coverage Precision Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6Fff2d_COVERAGE_PRECISION_PLAN.md`

## Requirement Status

- Complete: Added a regression test for `19,799/20,000 = 98.995%`, which is
  below `99%` but rounds to `99.00%` at two decimals.
- Complete: Preserved raw threshold comparison behavior; the near-threshold
  report still exits `1`.
- Complete: Failing combined coverage now uses extra precision when the
  two-decimal display would match the required threshold.
- Complete: Normal passing and clearly failing output remains at the existing
  two-decimal format.
- Complete: Ran only focused local validation for this script and its tests.
  Full AWF/GitHub validation remains managed by AWF after agent completion.

## Evidence

Files changed:

- `scripts/ci/check_coverage_threshold.py`
- `tests/unit/scripts/test_check_coverage_threshold.py`
- `plans/PRRT_kwDOSJAM6s6Fff2d_COVERAGE_PRECISION_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Fff2d_COVERAGE_PRECISION_VALIDATION.md`

Focused checks:

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py::test_checker_failure_reports_unrounded_combined_coverage_near_threshold -q`
  failed because stdout printed `combined=99.00%` and stderr printed
  `Combined line+branch coverage 99.00% is below required 99.00%`.
- During implementation, this same regression test was renamed from
  `test_checker_failure_reports_unrounded_combined_coverage_near_threshold`
  to
  `test_checker_failure_reports_detailed_combined_coverage_near_threshold`
  to match the final diagnostic behavior.
- After implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py::test_checker_failure_reports_detailed_combined_coverage_near_threshold -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py -q`
  passed with 10 tests.
- `uv run --python 3.12 --extra dev ruff check scripts/ci/check_coverage_threshold.py tests/unit/scripts/test_check_coverage_threshold.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check scripts/ci/check_coverage_threshold.py tests/unit/scripts/test_check_coverage_threshold.py`
  passed after applying focused Ruff formatting.

## Gaps

No gaps found.
