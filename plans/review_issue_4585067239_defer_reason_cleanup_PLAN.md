# Review Issue 4585067239 Defer Reason Cleanup Plan

## Problem Statement and Scope

Review feedback reports that `_clear_addressed_state_by_id` clears verdict,
body-hash, and needs-human reason state for a review item, but leaves the
pre-existing `__defer_reason__:<thread_id>` marker behind. A workflow-scope
push requeue can therefore leave a stale defer reason after the same thread is
later re-addressed with a non-defer verdict.

Scope is limited to PR monitor state cleanup for addressed review item IDs and
focused regression coverage for workflow-scope requeue behavior.

## Requirements Checklist

- Add a regression assertion that workflow-scope requeue removes stale
  `__defer_reason__` state for an inline thread.
- Update `_clear_addressed_state_by_id` to remove the defer-reason key for the
  item being cleared.
- Preserve deferred issue filed markers and existing false-positive review
  comment preservation behavior.
- Run only focused local validation for the changed monitor helper behavior.

## Implementation Steps

1. Add the failing regression assertion to the existing focused workflow-scope
   requeue test.
2. Run that single test and confirm it fails on the stale defer-reason marker.
3. Add `_defer_reason_state_key(item_id)` cleanup to
   `_clear_addressed_state_by_id`.
4. Re-run the focused test and any narrow adjacent test needed to cover the
   edited path.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k workflow_scope_requeue_clears_inline_threads_dependent_on_resolution`
  - Passes after the helper cleanup change.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
