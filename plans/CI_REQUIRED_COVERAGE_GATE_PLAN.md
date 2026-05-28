# CI Required Coverage Gate Plan

## Problem

PR #292 merged even though the `python-full-coverage` log reported:

```text
FAIL Required test coverage of 99% not reached. Total coverage: 98.87%
```

The GitHub job still concluded `success`, so the `ci-required` rollup saw
`PYTHON_FULL_COVERAGE_RESULT=success` and allowed branch protection/AWF to treat
the PR as mergeable.

## Requirements

- Preserve `ci-required` as the branch-protection rollup job.
- Make `python-full-coverage` fail whenever combined line+branch coverage is
  below 99%, even if pytest-cov exits zero after printing a fail-under message.
- Emit a clear GitHub Actions error line that AWF PR monitor log collection can
  pass to the agent LLM.
- Parse GitHub Actions `::error` annotations as structured CI failure evidence
  so the agent prompt highlights the coverage threshold reason before raw logs.
- Add focused coverage recovery tests for the companion-resume helper edges that
  PR #292 left short, so the gate PR has meaningful coverage lift instead of
  merely adding a stricter check.
- Keep the full coverage artifact upload on `always()`.
- Add focused regression coverage for the exact gate and workflow wiring.

## Implementation Steps

1. Add a small CI helper that parses `coverage.xml` and computes the same
   combined line+branch percentage coverage.py uses for branch coverage.
2. Add an `Enforce exact coverage threshold` step after `Full coverage` in
   `.github/workflows/ci.yml`.
3. Set coverage report precision to two decimals so the terminal summary no
   longer rounds a below-threshold combined total to `99%`.
4. Teach AWF CI failure evidence extraction to preserve GitHub Actions error
   annotations as structured error summaries.
5. Add companion-resume helper tests around Compose parsing, no-op refreshes,
   write-failure handling, atomic cleanup, and environment map/list helpers.
6. Extend workflow tests to require the exact gate and `ci-required` rollup.
7. Add helper tests for pass, fail, branch-aware combined totals, and invalid
   coverage XML.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/scripts/test_check_coverage_threshold.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_monitor_prompts.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_monitor_prompts.py -q`
- `uv run --python 3.12 --extra dev ruff check scripts/check_coverage_threshold.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/scripts/test_check_coverage_threshold.py src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_monitor_prompts.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
- `uv run --python 3.12 --extra dev mypy scripts/check_coverage_threshold.py`
