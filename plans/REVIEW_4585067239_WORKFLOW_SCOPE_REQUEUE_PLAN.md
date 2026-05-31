# Review 4585067239 Workflow-Scope Requeue Plan

## Problem Statement and Scope

Greptile flagged `_requeue_workflow_scope_publish_dependent_items` because it
accepts `inline_thread_ids` and immediately deletes it. That makes workflow-scope
push-failure rollback indistinguishable from ordinary push-failure rollback and
hides the intended distinction from linters and future callers.

Scope is limited to the PR monitor fix-cycle rollback behavior and focused unit
coverage for workflow-scope push failures.

## Requirements Checklist

- Use `inline_thread_ids` meaningfully in workflow-scope rollback.
- Clear publish-dependent inline review-thread state after a workflow-scope push
  rejection so unresolved inline threads are re-addressed after operator action.
- Preserve already recorded review-comment false-positive or defer verdict state
  when those verdicts do not depend on a successful workflow-file push.
- Continue clearing review-comment `fix_committed` state when the corresponding
  fix commit failed to publish.
- Keep deferred inline-thread filed-issue idempotency markers intact.
- Avoid broad AWF/GitHub-owned validation; run only focused tests for touched
  behavior.

## Implementation Steps

1. Update `_requeue_workflow_scope_publish_dependent_items` to clear only item
   IDs that are inline thread IDs for workflow-scope rollback.
2. Add or update focused tests covering:
   - inline thread state is cleared while review-comment false-positive state is
     preserved;
   - review-comment `fix_committed` state is still cleared when no inline thread
     ID protects it.
3. Run the focused pytest selection for the updated tests.
4. Record validation evidence in
   `plans/REVIEW_4585067239_WORKFLOW_SCOPE_REQUEUE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`

Pass criteria: all focused tests in the touched file pass. Full AWF/GitHub
validation remains managed by AWF after agent completion per the workspace
contract.
