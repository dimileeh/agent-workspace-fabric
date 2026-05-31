# PRRT_kwDOSJAM6s6F7neo Head-Only Settle Freeze Plan

## Problem Statement and Scope

An unresolved PR review thread reports that operator remonitor freeze state is
not honored when the preexisting reviewer-settle completion marker is a legacy
head-scoped `done_key` and the refreshed GitHub PR status includes
`quiet_period_anchor_at`. In that case, the monitor routes missing non-check
reviewers through the activity-anchored settle path, which currently only reads
activity-scoped freeze start markers.

Scope is limited to preserving the remonitor freeze for that head-only marker
case and proving it with a focused unit regression.

## Requirements Checklist

- Add a failing regression test for a head-scoped elapsed settle marker that is
  re-armed by `arm_operator_hint_freeze` and then evaluated with an activity
  quiet-period anchor.
- Ensure the activity-anchored non-check reviewer settle path waits from the
  remonitor freeze start instead of immediately treating the old activity anchor
  as elapsed.
- Preserve existing behavior for activity-scoped settle markers and normal
  activity quiet-period waits.
- Keep changes scoped to runtime monitor state helpers/tests.

## Implementation Steps

1. Add a regression test in
   `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`.
2. Run the new focused test and confirm it fails before the fix when practical.
3. Update the non-check reviewer settle logic so an armed head freeze can seed
   the current activity-scoped started marker from the head-scoped started
   marker.
4. Run the focused regression and nearby existing settle tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k "remonitor_freeze_rearms_head_only_elapsed_settle_with_activity_anchor or remonitor_freeze_rearms_activity_anchored_elapsed_settle or activity_anchored_freeze_elapsed_clears_head_freeze_marker"`

Pass criteria: the focused tests pass. Full AWF/GitHub validation is managed by
AWF after agent completion per the workspace contract.
