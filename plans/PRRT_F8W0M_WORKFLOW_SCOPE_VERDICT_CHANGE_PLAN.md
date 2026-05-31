# PRRT F8W0M Workflow Scope Verdict Change Plan

## Problem Statement and Scope

An inline review thread can be addressed more than once in a single fix cycle when new feedback arrives during the settle window. If an earlier pass marks the thread `fix_committed` and a later pass marks the same thread `false_positive`, stale workflow-scope publish bookkeeping can make a workflow-scope push failure overwrite the latest verdict with `needs_human`.

Scope is limited to PR monitor fix-cycle bookkeeping for re-addressed review items and a focused regression test.

## Requirements Checklist

- Add a regression test that reproduces `fix_committed` followed by `false_positive` for the same inline thread in one fix cycle.
- Ensure the latest verdict controls workflow-scope push-failure rollback/requeue behavior.
- Preserve existing workflow-scope handling for current `fix_committed` items.
- Run only focused validation for the changed area; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a failing unit regression near the existing workflow-scope fix-cycle tests.
2. Clear stale pending publish/resolve bookkeeping when a review item is re-addressed before recording the latest verdict.
3. Run the targeted test file or targeted test selection.
4. Record validation evidence in `plans/PRRT_F8W0M_WORKFLOW_SCOPE_VERDICT_CHANGE_VALIDATION.md`.
