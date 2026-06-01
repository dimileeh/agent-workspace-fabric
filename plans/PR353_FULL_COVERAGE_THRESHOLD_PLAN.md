# PR353 Full Coverage Threshold Plan

## Problem Statement and Scope

PR #353 fails GitHub Actions because `python-full-coverage` completed the test
suite successfully but reported total coverage of `98.99%`, below the required
`99%` gate. The required job aggregator fails only because that coverage job
failed.

Scope is limited to adding focused unit coverage for currently uncovered
behavior. Workflow files, quality gates, thresholds, and broad validation
configuration are out of scope and must not be weakened.

## Requirements Checklist

- [x] Preserve the current coverage gate and CI workflow behavior.
- [x] Add meaningful focused unit coverage for uncovered production behavior.
- [x] Keep changes scoped to tests and plan/validation artifacts unless a real
      production bug is found.
- [x] Run focused local tests for the changed tests only.
- [x] Record that full AWF/GitHub validation is owned by AWF after agent
      completion.

## Implementation Steps

1. Use the CI coverage artifact to identify uncovered line or branch
   opportunities that can be tested with narrow unit tests.
2. Add a pre-commit autofix parser test for a deterministic hook section that
   lacks the autofix marker while a later semantic hook has the marker.
3. Add a validation setup parser test for a Python command that exhausts
   interpreter options without finding `-m pip`.
4. Run the two focused test targets that cover the new assertions.
5. Save validation evidence in `plans/PR353_FULL_COVERAGE_THRESHOLD_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_commit_autofix.py::<new-test> tests/unit/runtime/test_validation_parts/test_validation_part_001.py::<updated-test> -q`
  - Passes locally.
- Full `python-full-coverage` is not run locally because AWF/GitHub own broad
  validation and coverage provenance after agent completion.
