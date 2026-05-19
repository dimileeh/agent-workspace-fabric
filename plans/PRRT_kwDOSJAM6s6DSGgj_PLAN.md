# PRRT_kwDOSJAM6s6DSGgj Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DSGgj` reports that
`PullRequestMonitorRunner._git_show_text` and `_git_diff_text` invoke `git`
without the shared `safe.directory` configuration used elsewhere. The scope is
limited to adding that safety configuration to these monitor-runner helpers and
covering the behavior with a regression test.

## Requirements Checklist

- Add a failing regression test proving both helper commands include
  `safe.directory` for the target worktree.
- Use the existing shared `git_safe_directory_config_args` helper rather than
  duplicating the argument construction.
- Keep the change scoped to PR monitor protected-file git helper behavior.
- Run the narrow regression test and relevant static checks.

## Implementation Steps

1. Add a unit test that calls `_git_show_text` and `_git_diff_text` with a fake
   command runner and asserts the recorded git argv includes the expected
   `safe.directory` arguments.
2. Confirm the new test fails before implementation.
3. Import `git_safe_directory_config_args` in `src/awf/runtime/pr_monitor_runner.py`.
4. Insert the helper output into both git command argv lists before `-C`.
5. Run the new unit test, then run targeted lint/type checks for the touched
   files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py -q -k git_helpers_mark_worktree_safe`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner.py`
  - Passes with no diagnostics.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes with no type errors.
