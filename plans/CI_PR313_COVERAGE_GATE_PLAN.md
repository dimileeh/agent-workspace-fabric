# CI PR313 Coverage Gate Plan

## Problem Statement and Scope

PR #313 fails GitHub Actions because the `python-full-coverage` job passed all
tests but reported total coverage of 98.99%, below the required 99% threshold.
The `ci-required` failure is a downstream aggregation failure. The scope is to
raise coverage with focused tests for PR-owned behavior without weakening CI,
coverage configuration, or workflow gates.

## Requirements Checklist

- [ ] Do not edit protected workflow, quality-gate, or configuration files.
- [ ] Do not run broad AWF/GitHub-owned validation locally.
- [ ] Add targeted tests for real behavior in changed Python code.
- [ ] Keep the fix scoped to the coverage failure.
- [ ] Commit the local fix with a conventional commit message describing the CI
      check and root cause.

## Implementation Steps

1. Inspect the failed GitHub Actions job log to identify the concrete coverage
   failure and missed lines.
2. Add focused unit coverage for uncovered `awf.common.owned_paths` branches:
   fixed custom planning templates, empty configured artifact entries, custom
   wildcard artifact entries, and non-workspace-id glob pattern handling.
3. Run focused tests for the changed test module and a focused coverage report
   for `awf.common.owned_paths`.
4. Record validation evidence in `plans/CI_PR313_COVERAGE_GATE_VALIDATION.md`.
5. Commit the plan, tests, validation, and no unrelated files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  must pass.
- A focused coverage check for `awf.common.owned_paths` may be run without a
  fail-under gate to verify the previously missed lines are covered.
- Full AWF/GitHub validation and the full coverage gate remain managed by AWF
  after agent completion.
