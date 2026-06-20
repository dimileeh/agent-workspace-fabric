# PR614 Current Head CI Repair Validation

Plan reference: `plans/PR614_CURRENT_HEAD_CI_REPAIR_PLAN.md`

## Requirement Status

- Identify concrete failing check names, run URLs, and root causes: Complete.
  - Current run `27846444397` failed `lint-and-type` at job
    `82416600959`: dependency install failed fetching `pluggy-1.6.0`
    metadata from PyPI with `stream closed because of a broken pipe`.
    This is an external transport failure, not a lint/type error.
  - Recent completed run `27844949199` failed
    `python-coverage-shards (8)` at job `82412078022` because
    `test_first_party_code_files_stay_under_line_limit` found oversized
    files.
- If the root cause is a behavior regression, add or adjust focused tests:
  Complete.
  - The actionable failure was a maintainability/line-limit regression, so the
    existing tests were split without changing assertions.
- Implement only the minimal fix for the confirmed root cause: Complete.
  - Split trailing tests out of two oversized test files.
  - Extracted mirror hook repair helpers from `execution_flow.py` to keep the
    source file under the same line limit while preserving the existing
    monkeypatch point for focused executor tests.
- Run focused local verification for the touched behavior: Complete.
- Create this validation file with evidence: Complete.
- Commit the local fix with a conventional commit message: Complete.

## Files Changed

- `src/awf/control/executor/execution_flow.py`
- `src/awf/control/executor/mirror_hooks_repair.py`
- `tests/unit/node/test_git_manager.py`
- `tests/unit/node/test_git_manager_mirror_hooks_path_errors.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_025.py`
- `plans/PR614_CURRENT_HEAD_CI_REPAIR_PLAN.md`
- `plans/PR614_CURRENT_HEAD_CI_REPAIR_VALIDATION.md`

## Evidence

- `wc -l src/awf/control/executor/execution_flow.py src/awf/control/executor/mirror_hooks_repair.py tests/unit/node/test_git_manager.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  - `execution_flow.py`: 1465 lines.
  - `mirror_hooks_repair.py`: 96 lines.
  - `test_git_manager.py`: 1462 lines.
  - `test_pr_monitor_runner_coverage_edges_part_020.py`: 1467 lines.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/node/test_git_manager_mirror_hooks_path_errors.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_025.py -q`
  - Passed: `4 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  - Passed: `6 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/node/test_git_manager_mirror_hooks_path_errors.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_025.py -q`
  - Passed after the final helper compatibility update: `10 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py src/awf/control/executor/mirror_hooks_repair.py tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_mirror_hooks_path_errors.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_025.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_flow.py src/awf/control/executor/mirror_hooks_repair.py`
  - Passed: no issues found in 2 source files.
- `git diff --check`
  - Passed.

Full AWF/GitHub validation, including coverage aggregation and a fresh CI run,
remains owned by AWF after agent completion.
