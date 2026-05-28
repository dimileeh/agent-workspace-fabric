# CI Required Coverage Gate Validation

Plan reference: `plans/CI_REQUIRED_COVERAGE_GATE_PLAN.md`

## Requirement Status

- Preserve `ci-required` as branch-protection rollup: Complete.
  `.github/workflows/ci.yml` still rolls up `lint-and-type`,
  `python-full-coverage`, `console`, and `release-artifacts`.
- Fail `python-full-coverage` when combined line+branch coverage is below 99:
  Complete. Added `scripts/ci/check_coverage_threshold.py` and wired it after the
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
- Restore local coverage above the exact 99 percent combined threshold:
  Complete. Added focused tests for companion schema/runtime edge branches,
  coverage command planning parse fallbacks, and failed-workspace remonitor
  candidate reopening.
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
- Ran local full coverage with 20 workers after coverage recovery:
  - pytest result: `8284 passed, 1 skipped`
  - pytest-cov total: `99.01%`
  - exact helper total: `combined=99.01% line=99.50% branch=97.39% covered=55110/55663`
  - skipped test: local macOS runner lacks the GitHub Actions passwordless-sudo
    setup required by `tests/integration/test_workspace_agent_git_in_workspace.py`

## Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_012.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/scripts/test_check_coverage_threshold.py tests/unit/docs/test_api_surface_cleanup_docs.py tests/unit/test_core_decomposition_maintainability.py tests/unit/node/test_companion_services.py tests/unit/api/test_schema_coverage_edges.py tests/unit/runtime/test_validation_coverage_gaps.py -q`
  - Result: `220 passed in 9.25s`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_003.py tests/unit/test_core_decomposition_maintainability.py -q`
  - Result: `10 passed in 4.54s`
- `uv run --python 3.12 --extra dev ruff check scripts/ci/check_coverage_threshold.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/scripts/test_check_coverage_threshold.py src/awf/runtime/ci_failure_evidence.py tests/unit/runtime/test_ci_failure_evidence.py tests/unit/runtime/test_monitor_prompts.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_012.py tests/unit/node/test_companion_services.py tests/unit/api/test_schema_coverage_edges.py tests/unit/runtime/test_validation_coverage_gaps.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_003.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy scripts/ci/check_coverage_threshold.py src/awf/runtime/ci_failure_evidence.py src/awf/node/companion_services.py src/awf/api/schemas_companions.py src/awf/runtime/validation_coverage.py`
  - Result: passed
- `uv run --python 3.12 --extra dev pytest -n 20 --dist=loadscope --timeout=300 --cov=awf --cov-report=term-missing --cov-report=xml --cov-fail-under=99`
  - Result: `8284 passed, 1 skipped in 833.45s`; pytest-cov total `99.01%`
- `uv run --python 3.12 python scripts/ci/check_coverage_threshold.py coverage.xml --minimum-percent 99`
  - Result: passed; `combined=99.01% line=99.50% branch=97.39% covered=55110/55663`
- `git diff --check`
  - Result: passed
