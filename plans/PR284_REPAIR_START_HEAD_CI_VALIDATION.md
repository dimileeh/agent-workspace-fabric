# PR284 Repair Start Head CI Validation

## Result

Implemented the CI repair for PR #284 by preserving the local worktree HEAD as
the repair transaction baseline whenever a worktree exists. The PR status/open
merge-candidate head is used only as a no-worktree fallback for helper paths.

## Checks

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Passed: 178 tests, including the cleanup-evidence regression for the PR
    review follow-up.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py tests/unit/runtime/test_pr_monitor_runner.py tests/integration/runtime/test_pr_monitor_runner.py -q`
  - Passed: 207 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_action_logging.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_rollback_distinguishes_reset_from_incomplete_cleanup_evidence tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_protected_scope_rollback_failed_reset_omits_unattempted_clean_result tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_execute_ci_fix_rolls_back_whole_delta_when_local_commit_touches_protected_scope tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_rolls_back_instead_of_committing_verified_protected_revert tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py::test_ci_fix_rolls_back_before_protected_revert_baseline_fetch -q`
  - Passed: 5 tests.
- `git diff --check`
  - Passed.

## Notes

The previous GitHub CI run for PR #284 failed before this fix at run
`26333237153`, specifically in `python-full-coverage`. The local focused monitor
surface now covers the failure class that produced those coverage failures.
Full coverage remains owned by GitHub CI after the repair commit is pushed.
