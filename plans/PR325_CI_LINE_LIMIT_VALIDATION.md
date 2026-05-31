# PR325 CI Line Limit Validation

Plan reference: `plans/PR325_CI_LINE_LIMIT_PLAN.md`

## Requirement Status

- Complete: Keep all first-party source and test files at or below 1,500 lines.
  Evidence:
  - `src/awf/runtime/pr_monitor_runner/helpers.py`: 1,152 lines
  - `src/awf/runtime/pr_monitor_runner/reviewer_settle.py`: 397 lines
  - `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py`: 1,378 lines
  - `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_007.py`: 185 lines
- Complete: Preserve the existing `awf.runtime.pr_monitor_runner.helpers`
  import surface used by runtime modules and tests.
  Evidence: moved non-check reviewer settle helpers are explicitly aliased from
  `helpers.py`.
- Complete: Move behavior mechanically without changing PR monitor decisions or
  test assertions.
  Evidence: extracted helper behavior and affected runner tests pass.
- Complete: Run the provided focused repro before and after the fix.
  Evidence: initial repro failed with oversized files; final repro passed.
- Complete: Avoid broad AWF/GitHub-owned validation locally.
  Evidence: only focused pytest and focused ruff commands were run. Full
  coverage and CI-required provenance are left to AWF/GitHub after agent
  completion.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Before fix: failed with `helpers.py` and `test_pr_monitor_runner_part_001.py`
    over 1,500 lines.
  - After fix: `1 passed in 0.42s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q`
  - Result: `27 passed in 12.37s`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_007.py -q`
  - Result: `30 passed in 16.69s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/reviewer_settle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_007.py`
  - Result: `All checks passed!`.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/reviewer_settle.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_007.py`
  - Result: `4 files already formatted`.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/helpers.py src/awf/runtime/pr_monitor_runner/reviewer_settle.py`
  - Result: `Success: no issues found in 2 source files`.
- `git diff --check`
  - Result: passed.

## Gaps

None for the scoped CI failure. Full AWF/GitHub validation was not run locally
because the workspace contract assigns broad validation, coverage gates, and
merge provenance to AWF/GitHub after agent completion.
