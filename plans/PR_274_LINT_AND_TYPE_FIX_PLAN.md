# PR 274 lint-and-type fix plan

## Problem statement and scope
The latest PR 274 CI run (`26228649141`) fails in `lint-and-type` with `ruff` errors, blocking the branch. Scope is limited to the minimal code-level fixes required to restore lint/type checks in touched Python modules.

## Requirements
- [ ] Restore valid syntax in `src/awf/runtime/pr_monitor_runner.py` around the CI fix failure path.
- [ ] Resolve all `ruff` `I001` import sorting violations introduced/left unformatted in `src/awf/control/executor.py` and `src/awf/service/doctor/reasons.py`.
- [ ] Ensure `ruff check` on changed files passes.
- [ ] Resolve unresolved `_MonitorAgentRuntimeOwnershipRepairFailedError` references in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`.
- [ ] Fix `I001` import order in
  `tests/unit/control/test_executor_monitor_recovery.py`.

## Implementation steps
1. Fix indentation/parenthesis in `_sync_base` error-handling block in `src/awf/runtime/pr_monitor_runner.py` so `try/except` nesting is syntactically valid.
2. Reorder imports in `src/awf/control/executor.py` and `src/awf/service/doctor/reasons.py` to match Ruff/Isort expectations.
3. Re-run focused lint to confirm no outstanding issues from the above files.
4. Replace remaining `_MonitorAgentRuntimeOwnershipRepairFailed` references in
   `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py` with the renamed
   `_MonitorAgentRuntimeOwnershipRepairFailedError` and re-run focused lint.
5. Re-run Ruff import sorting on
   `tests/unit/control/test_executor_monitor_recovery.py` and formatting on touched test runtime files.

## Verification plan
- Command: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py src/awf/control/executor.py src/awf/service/doctor/reasons.py`
- Pass criteria: zero diagnostics.
- Command: `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_monitor_recovery.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- Pass criteria: zero diagnostics.
- Command: `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor_runner.py src/awf/control/executor.py src/awf/service/doctor/reasons.py tests/unit/control/test_executor_monitor_recovery.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`
- Pass criteria: files already formatted.
