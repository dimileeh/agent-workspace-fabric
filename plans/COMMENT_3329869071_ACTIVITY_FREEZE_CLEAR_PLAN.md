# COMMENT_3329869071_ACTIVITY_FREEZE_CLEAR Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F7EqL` reports that the
activity-anchored non-check reviewer settle path marks only the
activity-specific elapsed key when a remonitor freeze expires. The
head-scoped freeze marker remains armed, so a later poll with the configured
reviewer visible as a check can re-enter the head-scoped freeze branch instead
of recognizing that the remonitor cooldown already elapsed. The fix is scoped
to the settle helper and focused unit coverage.

## Requirements Checklist

- Add a regression that fails when an activity-anchored remonitor freeze
  elapses but leaves the head freeze marker armed.
- Clear the head-scoped non-check reviewer freeze marker when the
  activity-anchored freeze window elapses.
- Preserve the activity-specific done marker and existing settle decision
  payload fields.
- Run only focused local validation; AWF/GitHub own broad validation after
  agent completion.

## Implementation Steps

1. Add a focused regression in
   `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`.
2. Update `src/awf/runtime/pr_monitor_runner/helpers.py` so the
   activity-freeze elapsed path clears `_non_check_reviewer_settle_freeze_key`.
3. Run the targeted regression, the nearby focused tests for non-check reviewer
   settle behavior, and a focused lint check on touched Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k "activity_anchored_freeze"`
  fails before the implementation and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k "remonitor_freeze or visible_check_remonitor_freeze"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/helpers.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`
  passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase per
  the workspace contract.
