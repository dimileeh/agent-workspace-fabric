# Review Issue 4590903660 Repo Flags Transient Validation

Plan reference: `plans/review_issue_4590903660_repo_flags_transient_PLAN.md`

## Requirement Status

- Complete: Missing repository merge-method flags remain an error, not an
  empty policy. The code still raises `GitHubClientError` when any required
  merge flag is omitted.
- Complete: The error is distinguishable from success. Missing/partial flag
  paths now raise with `returncode=1`, and focused tests assert that value.
- Complete: The error text is recognizable by the existing transient GitHub
  classifier. The message includes existing transient markers, and the focused
  classifier regression asserts this exact shape is transient.
- Complete: Explicit false merge flags still return an empty tuple. The
  existing focused `fetch_repo_merge_methods` selection passed with that case.
- Complete: Local checks were focused. Full AWF/GitHub validation was not run;
  AWF owns broad validation after agent completion.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py`
- `plans/review_issue_4590903660_repo_flags_transient_PLAN.md`
- `plans/review_issue_4590903660_repo_flags_transient_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k fetch_repo_merge_methods`
  - Result: `4 passed, 46 deselected`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py -q -k transient_github_error_classifier_keeps_auth_errors_terminal`
  - Result: `1 passed, 27 deselected`

## Gaps

No planned gaps remain.
