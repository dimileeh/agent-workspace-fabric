# PRRT_kwDOSJAM6s6F6r2U Non-Check Reviewer Settle Plan

## Problem Statement and Scope

The merge critical section rechecks PR status after the pre-merge settle wait,
but the follow-up non-check-reviewer settle decision only runs when operator
state changed. A refreshed PR status can contain a newer external reviewer
quiet-period anchor even when operator state did not change, so the monitor can
merge without re-arming the settle wait.

Scope is limited to the merge-loop recheck path and a regression test for the
review thread PRRT_kwDOSJAM6s6F6r2U.

## Requirements Checklist

- Add a regression test where the initial merge-ready snapshot is past the
  non-check reviewer quiet window, then the pre-merge status refresh reveals a
  newer external reviewer activity anchor with no operator-state refresh.
- Ensure the merge loop re-runs the non-check-reviewer settle decision after a
  successful pre-merge status refresh under the existing error, action, and
  initial-review-grace guards.
- Preserve existing wait operation behavior by assigning
  `settle_recheck_decision` when the refreshed settle decision has
  `wait_seconds > 0`.
- Keep changes scoped to PR-monitor merge behavior and its focused tests.
- Do not run broad AWF/GitHub validation; record only focused local checks.

## Implementation Steps

1. Add the failing regression test in
   `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`.
2. Run that single test to confirm it fails on the current code when practical.
3. Update `src/awf/runtime/pr_monitor_runner/merge_loop.py` so the refreshed
   status path evaluates the non-check-reviewer settle decision without relying
   on `operator_state_refreshed`.
4. Run the focused regression test, then the focused non-check-reviewer settle
   unit test file if runtime remains reasonable.
5. Write validation notes in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -k pre_merge_status_refresh_rearms_non_check_reviewer_settle -q`
  must fail before the fix and pass after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q`
  should pass after the fix.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
