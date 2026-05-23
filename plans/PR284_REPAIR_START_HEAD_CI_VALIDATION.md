# PR284 Repair Start Head CI Validation

## Result

Implemented the CI repair for PR #284 by preserving the local worktree HEAD as
the repair transaction baseline whenever a worktree exists. The PR status/open
merge-candidate head is used only as a no-worktree fallback for helper paths.

## Checks

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q`
  - Passed: 177 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_action_logging.py tests/unit/runtime/test_pr_monitor_runner.py tests/integration/runtime/test_pr_monitor_runner.py -q`
  - Passed: 207 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/runtime/test_monitor_action_logging.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner.py`
  - Passed.
- `git diff --check`
  - Passed.

## Notes

The previous GitHub CI run for PR #284 failed before this fix at run
`26333237153`, specifically in `python-full-coverage`. The local focused monitor
surface now covers the failure class that produced those coverage failures.
Full coverage remains owned by GitHub CI after the repair commit is pushed.
