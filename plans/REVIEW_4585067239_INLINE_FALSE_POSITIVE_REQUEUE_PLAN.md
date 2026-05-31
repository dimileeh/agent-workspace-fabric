# Review 4585067239 Inline False-Positive Requeue Plan

## Problem Statement and Scope

Greptile's review-level comment reports that inline review threads marked
`false_positive` during a comment-repair fix cycle can remain permanently open
on GitHub when the same cycle also commits a workflow-file fix and GitHub
rejects the push for missing `workflow` scope.

Scope is limited to PR monitor comment-repair workflow-scope failure handling in
`src/awf/runtime/pr_monitor_runner/fix_cycle.py`, focused unit coverage, and
this plan/validation record. Protected workflow, quality-gate, and repository
configuration files are out of scope.

## Requirements Checklist

- Requeue inline `false_positive` threads after workflow-scope push rejection so
  unresolved GitHub threads can be addressed and resolved after operator action.
- Continue marking workflow-file `fix_committed` items as `needs_human` with the
  workflow-scope reason.
- Preserve captured inline `defer` state and its filed-issue idempotency marker.
- Preserve review-level false-positive resolution state, since those comments do
  not have a GraphQL thread-resolution step and are already durably recorded.
- Keep non-workflow push-failure rollback behavior unchanged.
- Run only focused local checks; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Update the mixed workflow-scope unit regression so a false-positive inline
   thread is cleared and returned to `AddressComments`, while the workflow fix
   thread remains `needs_human`.
2. Extend the workflow-scope requeue helper coverage to prove only
   resolution-dependent inline threads are cleared; captured defer and
   review-level false-positive state remain intact.
3. Implement a separate workflow-scope resolution-dependent inline-thread queue
   in `fix_cycle.py` and clear it on workflow-scope push failure.
4. Run the focused tests that cover the changed behavior and nearby preserved
   behavior.
5. Record validation evidence in
   `plans/REVIEW_4585067239_INLINE_FALSE_POSITIVE_REQUEUE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_requeues_false_positive_thread_state tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_requeue_marks_publish_dependent_fixes_needs_human tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_preserves_captured_defer_thread_state tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_preserves_false_positive_review_comment_resolution`

Pass criteria: focused tests pass. Broad test suites, coverage gates, and
CI-equivalent validation are intentionally left to AWF/GitHub after this agent
phase.
