# PR Create Transient Retry Validation

## Commands Run

- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_creator.py tests/unit/control/test_executor_parts/test_executor_part_005.py::TestFailurePaths::test_transient_pr_create_exhaustion_records_retry_evidence tests/unit/common/test_github_client_parts/test_github_client_part_001.py::TestListOpenPullRequestsForBranch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_transient_github_error_classifier_keeps_auth_errors_terminal -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_transient.py src/awf/runtime/pr_creator.py src/awf/runtime/pr_monitor_runner/constants.py src/awf/control/executor/pr_open_step.py src/awf/control/executor/execution_flow.py tests/unit/runtime/test_pr_creator.py tests/unit/control/test_executor_parts/test_executor_part_005.py`
- `uv run --python 3.12 --extra dev mypy src/awf/common/github_transient.py src/awf/runtime/pr_creator.py src/awf/runtime/pr_monitor_runner/constants.py src/awf/control/executor/pr_open_step.py src/awf/control/executor/execution_flow.py`

## Results

- Targeted pytest: `43 passed`.
- Ruff: passed.
- Mypy: passed on touched source files.

## Coverage Notes

- Covered transient `gh pr create` retry success.
- Covered transient `gh pr create` reconciliation with a same-repo open PR.
- Covered duplicate/already-exists reconciliation.
- Covered fork PR collision rejection.
- Covered deterministic GitHub create failure without retry/lookup.
- Covered executor audit evidence for exhausted transient PR creation.
