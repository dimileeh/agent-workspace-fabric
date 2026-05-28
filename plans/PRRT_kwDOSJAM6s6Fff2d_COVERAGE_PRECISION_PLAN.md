# PRRT_kwDOSJAM6s6Fff2d Coverage Precision Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6Fff2d` reports that
`scripts/ci/check_coverage_threshold.py` fails on raw combined coverage below
the configured threshold while printing the failing value rounded to the same
two-decimal display as the threshold. The failure evidence can say `99.00%` is
below `99.00%`, which is not actionable for the PR monitor or a human reviewer.

Scope is limited to the coverage threshold helper, its focused unit tests, and
this plan/validation documentation.

## Requirements Checklist

- [x] Add a regression test for a raw combined coverage value below `99` that
      rounds to `99.00%` at two decimals.
- [x] Preserve raw threshold comparison behavior; do not make rounded values
      pass.
- [x] Report enough precision in failing stdout/stderr evidence that the raw
      failing value is visible and distinct from the threshold.
- [x] Keep passing coverage output concise for normal cases.
- [x] Run only focused validation owned by this change; leave broad AWF/GitHub
      validation to AWF after agent completion.

## Implementation Steps

1. Add the failing regression to `tests/unit/scripts/test_check_coverage_threshold.py`.
2. Run the single new test and confirm it fails against current output.
3. Update `scripts/ci/check_coverage_threshold.py` formatting so failing values
   expose precision beyond two decimals.
4. Run the focused script tests and lint for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py::<new-test> -q`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_coverage_threshold.py -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check scripts/ci/check_coverage_threshold.py tests/unit/scripts/test_check_coverage_threshold.py`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff format --check scripts/ci/check_coverage_threshold.py tests/unit/scripts/test_check_coverage_threshold.py`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally not run during the agent phase.
