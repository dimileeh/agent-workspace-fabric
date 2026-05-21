# PR 274 lint-and-type fix validation

## Plan reference
- plans/PR_274_LINT_AND_TYPE_FIX_PLAN.md

## Requirement status
- Restore syntax in `src/awf/runtime/pr_monitor_runner.py`: **Complete**
- Fix import sorting in `src/awf/control/executor.py`: **Complete**
- Fix import sorting in `src/awf/service/doctor/reasons.py`: **Complete**
- Resolve ownership-repair exception renames in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`: **Complete**
- Fix import sorting in `tests/unit/control/test_executor_monitor_recovery.py`: **Complete**
- Ruff check focused files passes: **Complete**

## Evidence
- Commands run:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/control/executor.py src/awf/service/doctor/reasons.py`
    - Result: All checks passed
  - `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_monitor_recovery.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py --select I001,F821,SIM103`
    - Result: All checks passed
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/control/executor.py src/awf/service/doctor/reasons.py tests/unit/control/test_executor_monitor_recovery.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
    - Result: All checks passed
  - `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner.py src/awf/control/executor.py src/awf/service/doctor/reasons.py tests/unit/control/test_executor_monitor_recovery.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
    - Result: All checks passed
  - `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_worker.py`
    - Result: All checks passed
  - `uv run --python 3.12 --extra dev mypy src/awf`
    - Result: Success: no issues found in 160 source files
