# GitHub Operation Retry And Redundancy Validation

## Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_transient.py tests/unit/runtime/test_pr_creator.py tests/unit/runtime/test_release_pr_sync.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_017.py tests/unit/control/test_executor_parts/test_executor_part_005.py -q`
  - Result: `104 passed`
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_transient.py src/awf/runtime/release_pr_sync.py src/awf/runtime/pr_monitor_runner/helpers.py tests/unit/common/test_github_transient.py tests/unit/runtime/test_pr_creator.py tests/unit/runtime/test_release_pr_sync.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_017.py tests/unit/control/test_executor_parts/test_executor_part_005.py`
  - Result: passed
- `uv run --python 3.12 --extra dev mypy src/awf/common/github_transient.py src/awf/runtime/release_pr_sync.py src/awf/runtime/pr_monitor_runner/helpers.py`
  - Result: passed

## Notes

- The exact GitHub GraphQL malformed-request response with `Please try
  resubmitting` is now classified as transient only when GitHub API/GraphQL
  context is present.
- Feature PR creation, release PR creation, and PR monitor GitHub retry paths all
  have focused coverage for the new classification.
- Deterministic errors such as bad credentials and generic malformed HTTP 400
  responses remain non-transient.
