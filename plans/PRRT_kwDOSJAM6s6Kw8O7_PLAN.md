# PRRT_kwDOSJAM6s6Kw8O7 Plan

## Problem Statement And Scope

An inline review reports that `_commit_dirty_worktree` reports success after
missing-HEAD recovery even when the recovered-head `git diff --name-only -z`
command fails. Because the recovered path list is unavailable, the supply-chain
policy refresh is skipped while callers still proceed as if recovery was safe.

Scope is limited to the missing-HEAD recovery branch in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and focused regression
coverage for that behavior.

## Requirements

- Add a regression test showing recovered HEAD changes fail closed when the
  recovered-head diff command fails.
- Ensure the failing diff path does not report success to callers.
- Preserve existing successful recovered-diff gate ordering.
- Avoid broad validation; AWF/GitHub own full validation after this agent phase.

## Implementation Steps

1. Add a focused unit test near the existing missing-HEAD recovery tests.
2. Confirm the new test fails against the current implementation when practical.
3. Change `_commit_dirty_worktree` to return `False` with a warning when the
   recovered-head diff command fails.
4. Run the focused unit test file or specific test, plus focused lint/type checks
   for touched files if needed.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_fails_closed_when_recovered_diff_fails -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py`

Pass criteria: the focused regression passes, lint/type checks pass for the
touched files, and no broad AWF/GitHub validation is run locally.
