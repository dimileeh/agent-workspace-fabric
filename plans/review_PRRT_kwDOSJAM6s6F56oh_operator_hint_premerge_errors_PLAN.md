# Review PRRT_kwDOSJAM6s6F56oh Operator Hint Pre-Merge Errors Plan

## Problem Statement And Scope

The PR monitor merge path refreshes persisted operator remonitor hints inside the
merge critical section, but pre-merge recheck error handling can run before the
refreshed non-merge action is dispatched. If an operator hint arrives while a
pre-merge status refresh also fails, the monitor can terminate or retry the
workspace instead of prioritizing the operator-hint repair path.

Scope is limited to the PR monitor merge-loop ordering and focused regression
coverage for this review thread.

## Requirements Checklist

- Add a regression test proving a persisted pending operator hint refreshed
  during merge handling is dispatched even when the pre-merge recheck raises a
  terminal error.
- Preserve existing pre-merge recheck error behavior when no refreshed operator
  hint exists.
- Keep the implementation narrowly scoped to the merge-loop decision order.
- Run focused tests only; broad AWF/GitHub validation remains owned by AWF after
  the agent phase.

## Implementation Steps

1. Add a failing unit test around `PullRequestMonitorRunner._execute(Merge())`
   with pre-merge settle enabled, a recheck failure, and a concurrently
   persisted operator hint.
2. Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` so refreshed
   non-merge actions, including operator-hint repair, are dispatched before
   pre-merge error handling.
3. Run the focused test node, then run the smallest relevant neighboring test
   slice that covers pre-merge errors and operator hints.
4. Write validation evidence in the matching validation document.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py::<new-test-node> -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_pre_merge_recheck_github_error_fails_workspace tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_pre_merge_recheck_base_fetch_error_fails_workspace tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_001.py::test_pre_merge_recheck_base_behind_error_fails_workspace -q`

Pass criteria: the new regression fails before the implementation change,
passes after the implementation change, and the existing targeted error tests
continue to pass.
