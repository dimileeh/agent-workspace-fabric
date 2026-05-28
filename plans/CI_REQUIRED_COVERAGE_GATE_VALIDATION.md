# CI Required Coverage Gate Validation

Plan reference: `plans/CI_REQUIRED_COVERAGE_GATE_PLAN.md`

## Requirement Status

- Preserve `ci-required` as branch-protection rollup: Complete.
  `.github/workflows/ci.yml` still rolls up `lint-and-type`,
  `python-full-coverage`, `console`, and `release-artifacts`.
- Fail `python-full-coverage` when combined line+branch coverage is below 99:
  Complete. Added `scripts/check_coverage_threshold.py` and wired it after the
  full coverage step.
- Emit a clear GitHub Actions error for AWF/agent prompts: Complete. The helper
  emits `::error title=Coverage below required threshold::...`.
- Parse GitHub Actions `::error` annotations into AWF structured CI evidence:
  Complete. `src/awf/runtime/ci_failure_evidence.py` now treats annotations as
  error summaries, and monitor prompt tests prove the agent sees the coverage
  reason in the summary block.
- Add focused coverage recovery for PR #292 helper edges: Complete. Added tests
  for companion-resume Compose parsing, no-op refreshes, write failure logging,
  atomic temp-file cleanup, and env map/list helper branches.
- Keep artifact upload on `always()`: Complete. The upload step is unchanged.
- Focused regression coverage: Complete.

## Evidence

- Reproduced the reported CI run state:
  - `python-full-coverage` job conclusion was `success`.
  - Log contained `FAIL Required test coverage of 99% not reached. Total coverage: 98.87%`.
  - `ci-required` saw `PYTHON_FULL_COVERAGE_RESULT: success`.
- Downloaded the run artifact and confirmed exact totals:
  - line coverage: `99.40%`
  - branch coverage: `97.15%`
  - combined line+branch coverage: `98.87%`
- Ran the new helper against that artifact and confirmed it exits `1`.

## Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/scripts/test_check_coverage_threshold.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_monitor_prompts.py -q`
  - Result: `117 passed in 6.14s`
- `uv run --python 3.12 --extra dev ruff check scripts/check_coverage_threshold.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/scripts/test_check_coverage_threshold.py src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_monitor_prompts.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy scripts/check_coverage_threshold.py src/awf/runtime/ci_failure_evidence.py`
  - Result: passed
- `git diff --check`
  - Result: passed
