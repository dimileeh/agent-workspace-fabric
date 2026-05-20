# CI Full Coverage Fix Plan

## Problem Statement and Scope

PR #268 passes the Python test suite in GitHub Actions, but the
`python-full-coverage` job fails because total coverage is 98.26% while the CI
gate requires `--cov-fail-under=99`.

Scope is limited to restoring real coverage for the diff-aware protected
quality-gate changes. Do not disable, skip, weaken, or lower the CI coverage
gate.

## Requirements Checklist

- Identify uncovered behavior in the touched protected-file classifier surface.
- Add focused regression tests for meaningful uncovered branches instead of
  changing coverage configuration.
- Preserve the protected quality-gate behavior already introduced in PR #268.
- Keep branch management and pushing under AWF control.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Run focused coverage on the protected-file tests and touched modules to find
   missing branches/lines.
2. Add targeted tests in the existing quality-gate/protected-file test files for
   uncovered classifier, parsing, and diagnostic paths.
3. Re-run focused coverage and the relevant unit tests.
4. Run lint/type checks for the touched Python surface.
5. Create `plans/CI_FULL_COVERAGE_FIX_VALIDATION.md` with requirement status
   and command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py --cov=awf.control.quality_gates --cov=awf.control.protected_file_diffs --cov-report=term-missing --cov-fail-under=99 -q`
  - Passes and shows the protected quality-gate surface is back above the CI
    threshold locally.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Passes the PR's focused protected-file validation surface.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py src/awf/control/protected_file_diffs.py tests/unit/control/test_quality_gates.py tests/unit/control/test_protected_file_diffs.py`
  - Passes with no lint regressions.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes with no type regressions.
