# COMMENT_3329608288_GRACE_RECHECK Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F6SNs` reports that the merge critical section
can import a remonitor freeze that re-arms initial-review grace, then continue
to `merge_pr` without rechecking that grace window. The fix is scoped to the PR
monitor merge path and a focused regression test for the remonitor-freeze case.

## Requirements Checklist

- Add a regression that fails when a DB-imported remonitor freeze re-arms
  initial-review grace while configured non-check reviewers are already visible
  as checks.
- Recheck initial-review grace after refreshing operator/freeze state inside the
  merge critical section and before attempting `merge_pr`.
- Keep the lock held only for rechecks and merge eligibility decisions; perform
  waits after leaving the serialized merge section.
- Preserve existing non-check reviewer settle behavior and merge-gate handling.
- Run only focused local validation; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add a focused unit regression in `tests/unit/runtime/test_pr_monitor_operator_hints.py`.
2. Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` to calculate and
   honor an initial grace recheck after operator freeze import.
3. Run the targeted regression test, then the nearby operator-hints unit test
   file if the targeted test passes quickly.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "visible_reviewer_freeze"`
  reproduces the regression before the fix and passes after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q -k "merge_rechecks"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_operator_hints.py`
  passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase per
  the workspace contract.
